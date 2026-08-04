# AI Coding Agent Experiment Lab

同一の開発課題と品質Gateを使い、AIコーディングの「Workflow」と「Coding Agent
Provider」を再現可能な形で比較するためのR&D基盤です。仮説、実験条件、評価結果、
学習内容、標準手順を蓄積し、開発プロセスの改善判断を支援します。

## 解決する問題

AI支援開発の比較では、モデル、ツール、プロンプト、実行手順、課題、評価方法が同時に
変わりやすく、結果の原因を説明できなくなります。本プロジェクトは、実験Specと結果の
データ契約、Provider境界、固定条件を明示し、比較可能性と追跡可能性を確保します。

Workflow比較（`one_shot` / `staged`）とProvider比較（Codex CLI / Google
Antigravity CLI / Replay Provider）は分離します。一度に変える独立変数を一つに限定し、
差が実行手順によるものかProviderシステムによるものかを混同しないためです。

## 現在の状態

Phase 0〜4を完了しました。**Phase 5: Antigravity CLI Provider** はBlockedです。
2026-07-28に[offline設計](docs/ANTIGRAVITY_PROVIDER.md)を確定し、Antigravityへ渡す最初の
実装範囲をversion付き契約、strict stream parser、read-only preflight、redaction済み
Evidence、fake `agy`受入へ限定しました。2026-07-29にSlice 5Aの是正と受入テスト補完を
完了しました。2026-07-30に公式Antigravity CLI 1.1.8 `darwin_arm64` artifactを実行前に
受入検証し、manifest／payload checksumとarchive安全性はPASSしましたが、
`codesign --verify --deep --strict`が失敗したためbinaryを受け入れませんでした。
この外部blocker `upstream_artifact_signature_invalid`により、Antigravity Providerの
Slice 5B／5CだけをBlockedとします。安全Gateが未検証binaryの配置・実行前に停止した結果で
あり、プロジェクト全体や完了済みPhase 0〜4の状態は変わりません。詳細と再開条件は
[Phase 5オフライン設計](docs/ANTIGRAVITY_PROVIDER.md)を参照してください。stdin transportも
引き続き`not_verified`であり、
[Slice 5B Prompt Transport Decision Record](docs/decisions/SLICE_5B_PROMPT_TRANSPORT_DECISION.md)
を維持します。実Antigravity Provider call、認証、Prompt送信、quota利用、Live、
Provider比較は未着手です。
offline実装とfake Codexによる受入を完了し、レビュー済みcommit
`2abd653a7b42f8932c0005e6d7d3fdd1252845e0`に対する事前登録済みの実Codex Live A/B
Campaignを2026-07-28に1回だけ実行しました。予定6 run／6 Provider callsに対し、最初の
`staged` runを1回／1 call実行後、`harness_failure`で安全停止し、残り5 runは
`not_run`です。complete pairは0、reportは`not_estimable`なのでPhase 4はCurrentのままです。
Campaign 001のArtifactと失敗履歴は変更せず、判明したPython bytecode cache混入だけを
offlineで修正しました。Provider processとGate commandには、run専用system temporary
directory配下のHarness管理`PYTHONPYCACHEPREFIX`を明示し、bytecode cacheを評価Workspace
から分離します。未知のbinary変更は従来どおり不完全Evidenceとして拒否します。
Campaign 001は再実行しません。修正後の新しいLiveにも、新experiment ID、新Artifact root、
新canonical Plan、reviewed commit、別の明示承認が必要です。

bytecode cache修正後、reviewed commit
`edd8c9e748998d056efa70fa43a26d10aa8ded12`、canonical Plan SHA-256
`375675a105b3de6b371551ab09c25014e3198d256bf09717e80fe20e747125ee`のCampaign 002を、
2026-07-28T11:00:21.701522Zから11:05:11.761906Zまで1回だけ実行しました。planned／
attempted／completed／failed／not_runは6／6／6／0／0、actual Provider callsは6、
unknown callsは0、retry／fallback／resumeは0、stop reasonは`none`です。`one_shot`と
`staged`は各3 runが完了し、各runのacceptance／regression／lint／typecheckはすべてPASS
しました。scheduled／complete pairは3／3で`pairing.status=estimable`です。
report JSON／Markdown SHA-256はそれぞれ
`d819eb5a1403f623527dcf84c665e88f3ae0b49d6b0878d5dd9941c1f60f139a`／
`936a44a9710d1dbd16ec815a97fac190f3bad4366b27b8b02cf11f7bc5d4af4a`です。
ArtifactはGit管理外に保持し、Campaign 001の`harness_failure`履歴は不変です。Campaign 001も
Campaign 002も再実行しません。Phase 3 manual Live累計8試行は変更せず、Phase 4 Campaignの
call数と分離します。この1 Task、固定Prompt／Fixture／Gate、各3反復、当該環境・実行時期の
結果から、一般的なモデル性能、統計的有意差、普遍的なWorkflow優位性は主張しません。
Phase 4はComplete、Phase 5はBlockedです。

Phase 6はPhase 4 Completeへ依存し、Phase 5とは独立して開始しました。Slice 6Aのversion付き
契約とSlice 6Bのlocal Fixture Acceptanceに加え、Slice 6CではSpec 2.1／Plan 1.2のcreate-only
準備とPlan-bound Campaign runtimeを実装しました。Plan生成時、Campaign開始前、各call直前に
入力を照合し、Provider後・Gate前にDiff Policyを適用します。違反は
`output_contract_violation`としてGate 0件で拒否し、Gate後もWorkspaceを再検証します。
Slice 6Dでは保存済みの列挙Artifactだけを読む決定的なPublic Suite renderer、Historical
offline verifier、checksum／外部anchor、atomic create-only publisherを実装しました。
人間が最終AcceptanceしたPublic Suite
`phase6-java-evaluated-0e6d894d-001`を現在の公式status正本とし、Python／Javaは各1 Fixture・
1 complete pairで`evaluated`、Phase 6はengineering minimumを満たして`Complete`です。
TypeScriptは`typescript_compiler`未解決の`not_ready`かつPublic Suite未掲載、Antigravityは
`not_evaluated / upstream_artifact_signature_invalid`のままです。Phase 7は`Planned`です。

accepted Manifestは
`.artifacts/phase6/public-suite-inputs/phase6-java-evaluated-0e6d894d-001/suite-manifest.json`
（7,548 bytes、SHA-256
`88db41ae59fe03cff87d6d775cdded5dfdf6117bf73db02d36baad535a54b819`）です。14 filesのbundleは
`.artifacts/phase6/public-suite/phase6-java-evaluated-0e6d894d-001/bundle`へcreate-onlyで公開し、
`checksums.json`は2,268 bytes、SHA-256
`43352dc27e7f5ffca63b9bdd65a3e38b100b255241a6240d99905ce0dc21f526`です。bundle外の
`.artifacts/phase6/public-suite/phase6-java-evaluated-0e6d894d-001/bundle.checksums.sha256.json`
は259 bytes、SHA-256
`50de193368057c6e095ec7625a31b67d62c5cd90fd997975debe72931422b818`で、rendererとのbyte一致は
14/14です。Python／Javaのscheduled／complete pairは各1/1です。

Python primary Campaignは`phase6-python-workflow-independent-001`、Java primary Campaignは
`java-independent-004`（experiment ID `phase6-java-workflow`）です。Pythonのabandoned／
inconclusive Campaignと、Javaの`java-rebound-001`、`java-independent-002`、
`java-independent-003`は評価分母から除外し、削除せず監査Artifactとして保持します。累積
Provider accountingは`9または10 calls`で、不確定性は旧Python Campaignの0または1 callです。

Formal Workflow Reportは`report-workflow`が生成するCampaign単位の成果物です。Public Suiteの
language reportは保存Campaign／Evidenceからrendererが再導出した別の公開契約であり、Formal
ReportをManifest inputやbundleへ混入させていません。Public Suiteはcanonical JSON、
`checksums.json`、bundle外External Anchorで固定します。

Live運用ではCodex agent内のnested `codex exec`がpermission failureとなり、詳細なOS-level
root causeは未確定です。一方、Mac Terminalで明示的な絶対`CODEX_HOME`を設定したJava
Campaignは成功しました。現行運用ではLive CampaignだけをHost Terminalから実行し、sandbox
制約を回避する実装は行いません。offline準備、validation、Report、publication、監査はCodex
から実施できます。

この評価はPython／Javaそれぞれ1 Fixture・1 complete pairのone-shot／staged比較に限定します。
automatic winner、leaderboard、統計的有意性はなく、一般的なWorkflow、Provider、model性能の
優劣を主張しません。cached inputを無視した単純なtoken／コスト比較も行いません。詳細と
provenanceは[Phase 6詳細設計](docs/PHASE6_MULTI_LANGUAGE_PUBLIC_REPORT.md)を参照してください。

Phase 4までに次を提供します。

- バージョン付きExperimentSpec、RunMetrics、UsageMetrics、CapabilityReport
- YAML Specの検証
- Codex CLIとAntigravity CLIの読み取り専用能力確認
- 実験設計、Provider境界、ロードマップの文書
- Recording 1.0の`run_started`/`run_completed`契約と、Live 1.1の
  `run_started`/`run_completed|run_failed`契約
- 1件の保存済みRecordingから1件のRunResultを生成・保存するReplay CLI
- 信頼済みFixtureの使い捨てコピーと、Specに列挙されたargvだけを実行するlocal Runner
- timeout、process group停止、残存子process回収、環境変数allowlist
- 上限付きstdout/stderr、実行前後diff、終了状態を持つversion付きEvidence
- 通常のGate不合格とHarness障害を分ける`agentlab run-gates`
- read-only Codex CLI preflightと、明示確認が必要な`agentlab live-codex`
- stdin Prompt、上限付きincremental JSONL parser、redaction済みCodex Evidence
- 2イベントのLive Recording 1.1と、その成功記録のoffline Replay
- 正確なevaluation durationを持つLive Artifact 1.1と、1.0のstrict読込互換
- Provider成功後に同じ使い捨てWorkspaceで品質Gateを実行する最小vertical slice
- strict paired成果物を構築できない場合だけの独立したFailure Diagnostic 1.0
- 後方互換なExperimentSpec 1.0とは分離したstrict Workflow A/B Spec 2.0
- Task要件を共有しWorkflow指示だけを変えるversion付きPrompt builder
- SHA-256によるseed付きblock順序と、byte決定的なcanonical Plan 1.1
- 1 run = 1 Provider turn = 1 agent callを維持する逐次Campaign scheduler
- append-only Campaign 1.1、stop condition、固定run/failure/停止理由Enum
- 保存済みPlan、Campaign、Recording、Evidenceだけを読むJSON/Markdown集計

ReplayはRecording内の保存済みMetricsだけを再構成し、外部処理を呼びません。通常テストは
短時間のfake Codex executableだけを使い、実Codex、外部AI、network、quotaを呼びません。
Phase 3のmanual Liveは累計8試行です。006はProvider起動後の`model_access`境界、
007は`inconclusive_prompt_delivery_failure`として履歴を保持します。008は人間が
selectable catalogから明示選択した`gpt-5.6-sol`を、commit
`cc97e53bf0bac426b08346f63e6f527ed7d5be9e`のAgentLab製品経路で1回実行して成功しました。
agent callは1、retry／fallbackは0で、`turn.completed` 1件、Provider exit 0、
`task.txt`の期待した1行変更、4種類のQuality Gate全PASS、Evidence 1.5／Recording 1.1の
strict再読込、redaction、process／Workspace cleanup、成功Recordingのoffline Replay、
Replay Metrics一致を確認しました。この最小vertical sliceの結果は、一般的なモデル性能や
Provider比較結果を示すものではありません。Phase 4 Campaign 002では3組のpaired結果を
得ましたが、固定した1 Taskと各3反復の結果から普遍的なWorkflow優劣を示しません。
AntigravityのHeadless Runner／Live実行、Provider比較、dashboard、notebook、統計的検定、
並列schedulerは未実装です。Antigravity Slice 5Aのoffline preflight、strict parser、
Evidence、fake executable受入だけが実装済みです。

## Quick Start

Python 3.12以上と[uv](https://docs.astral.sh/uv/)を使う例です。

```console
uv sync --extra dev
uv run agentlab doctor
uv run agentlab doctor --json
uv run agentlab validate experiments/examples/workflow-smoke.yaml
uv run agentlab validate experiments/examples/codex-live-smoke.yaml
uv run agentlab validate-workflow experiments/examples/workflow-ab.yaml
uv run agentlab plan-workflow experiments/examples/workflow-ab.yaml \
  --output .artifacts/workflow-ab/plan.json
uv run agentlab replay experiments/examples/workflow-smoke.yaml \
  --output .artifacts/runs/workflow-smoke-run-001.json
uv run agentlab run-gates experiments/examples/workflow-smoke.yaml \
  --task-id smoke-task \
  --run-id phase2-runner-smoke-001 \
  --output .artifacts/evidence/phase2-runner-smoke-001.json \
  --confirm-execution
uv run pytest
uv run ruff check .
uv run mypy src
```

`plan-workflow`は外部AI、Provider、network、Gateを呼びません。canonical Planには現在時刻を
混入させず、同じSpec bytes、Task Prompt、Fixture、seedから同じbytesを生成します。作成時刻は
Plan SHA-256を持つ`plan.metadata.json`へ分離します。Planと予約Artifact pathはcreate-onlyで、
開始済みCampaignの変更や自動resumeは行いません。
Plan 1.1は`one_shot`／`staged`それぞれの生成Prompt SHA-256とbyte数も事前登録します。
Campaign開始時にTask Prompt bytesとFixture全file bytesを一度だけ固定し、全runのadapterを
そのin-memory snapshotから構築します。各run開始前のsource integrity checkで変更を検出した
場合は、次のProvider callを開始せず`input_changed`で停止します。

Live Campaignの形式は次です。事前登録済みCampaignは上記のとおり1回だけ実行し、再実行
しません。`--confirm-live-codex`と、
Planに表示された予定Provider call総数と一致する`--confirm-provider-calls`の両方がなければ、
version/help preflightを含むsubprocessを起動しません。
`experiments/examples/workflow-ab.yaml`のmodelはfake受入専用であり、実Codex Liveには
使用しません。Live前に人間がexact model IDを明示したレビュー済みSpecを別途事前登録します。

```console
uv run agentlab run-workflow-campaign <reviewed-phase4-spec.yaml> \
  --plan .artifacts/workflow-ab/plan.json \
  --campaign .artifacts/workflow-ab/campaign.jsonl \
  --confirm-live-codex \
  --confirm-provider-calls 6

uv run agentlab report-workflow <reviewed-phase4-spec.yaml> \
  --plan .artifacts/workflow-ab/plan.json \
  --campaign .artifacts/workflow-ab/campaign.jsonl \
  --output .artifacts/workflow-ab/report.json \
  --markdown .artifacts/workflow-ab/report.md
```

`one_shot`と`staged`はいずれも単一Prompt、単一Provider process、単一turn、agent call 1です。
`staged`の調査、計画、テスト確認・追加、実装、自己レビューは一つのturn内の論理段階で、
内部の計画、reasoning、agent message、stage出力は保存・評価しません。比較軸はWorkflow
Promptだけで、Provider、exact model ID、reasoning effort、Fixture、Gate、sandbox、
network設定、timeout、停止条件はCampaign全体で固定します。詳細は
[docs/WORKFLOW_AB.md](docs/WORKFLOW_AB.md)を参照してください。

adapter用Prompt fileはArtifact root外のsystem temporary directoryだけに作成します。
cleanup結果はCampaignへ`cleared`／`failed`として保存し、失敗時は`cleanup_failure`で即時停止
してPrompt fileをbest-effortでredactします。cleanup中の`KeyboardInterrupt`／`SystemExit`も
cleanup失敗としてCampaignを完結させてから元の中断を再送出します。offline reportはPlan、
Campaign、Evidence、Recordingのrun identity、outcome、Provider call数、Prompt/Fixture
fingerprint、model、reasoning effort、成功・失敗両方のevaluation summaryを相互照合し、
矛盾するpairを拒否します。

次はPhase 3で使用したmanual smokeのCLI形式です。実Codexを使うため、通常テストやCIでは
実行しません。承認済みmanual smokeは累計8試行で、過去runを再実行しません。新しい実行は
レビュー済みcommit、新しいSpec／run-id／出力先、個別の明示承認なしには行いません。

```console
uv run agentlab live-codex experiments/examples/codex-live-smoke.yaml \
  --task-id codex-live-smoke \
  --repetition-index 0 \
  --run-id codex-live-smoke-001 \
  --output .artifacts/evidence/codex-live-smoke-001.json \
  --confirm-live-codex
```

現在確認済みの`codex-cli 0.146.0-alpha.3.1`は
`headless_exec_explicit_never_v2` profileのversion allowlistと必須flagを満たし、
read-only preflightに成功します。このprofileは
`--config approval_policy="never"`をargvで明示し、存在しない
`--ask-for-approval`には依存しません。profile名、CLI version、明示設定の根拠を
Evidenceへ保存します。preflightを完了できない場合はprofileを`not_selected`とし、
approval policyを適用済みとは記録しません。preflight完了後もProvider起動前なら、選択済み
profileと確認済みflagを保持しつつ、approval policyはnullのままです。Codex Evidence 1.3は
runner、Popen試行、process生成、process group回収の観測状態を固定Enumで保持し、新規
Evidence 1.4はtop-level `error`をturn terminalと分離して数えます。Evidence 1.5は
PromptのOS stdin pipe書込みを`not_started`／`partial`／`complete`／`unknown`で観測し、
free-formなProvider失敗messageを保存せず固定Enumのadvisory hintへ分類します。このhintは
`failure_kind`や根本原因の代替ではありません。1.1〜1.4は従来の制約のままstrict load
できます。固定CLI sourceで到達可能なpre-turn warningと
`error`→`turn.failed`へparserをオフラインで合わせましたが、004の実際のevent列は
復元できず、この欠陥が004の直接原因だったとは断定しません。Failure Diagnosticは、
strict paired成果物を構築できない場合だけ固定Enumの観測値をatomic・上書き禁止で保存し、
Evidence／Recordingの代替でもReplay入力でもありません。

`--confirm-live-codex`なしではversion/help preflightを含むsubprocessを起動しません。
確認付き実行はCodex model APIへのPrompt送信とquota消費を伴います。Promptはargvへ
含めずstdinから渡し、Prompt本文、raw Codex JSONL、raw stderr、agent message、
reasoning、command output、thread/session IDをRecordingやEvidenceへ保存しません。
PromptはSHA-256とbyte数だけを保存します。
Evidence 1.5のstdin状態はHarnessがOS pipeへ書いたbyte数だけを表し、CodexがPromptを
読んだこと、model APIへ送ったこと、quotaを消費したことを証明しません。

Phase 3の認証対象は既存Codex CLIのChatGPT-managed authenticationだけです。
`OPENAI_API_KEY`と`CODEX_API_KEY`はProvider processへ継承せず、API key方式は未対応です。
Live実行前に`CODEX_HOME`が明示された絶対pathの既存directoryであることを要求し、
`HOME/.codex`へ暗黙fallbackしません。
Codex自身のmodel API通信は必要ですが、model-generated commandのnetwork accessと
web searchを無効化します。OS-levelの完全なnetwork遮断ではありません。詳細は
[docs/CODEX_PROVIDER.md](docs/CODEX_PROVIDER.md)を参照してください。

`doctor` はローカルコマンドの存在、バージョン、helpだけを確認します。ログイン、API呼び
出し、AIタスク実行はしません。`replay`も外部処理を実行せず、Specファイルからの相対
pathでRecordingを読みます。既存Resultを明示的に置換する場合だけ`--force`を指定します。
`--force`でも入力元のExperimentSpecやRecording、そのsymlink/hard linkは置換できません。

`run-gates`はlocal subprocessを起動するため、`--confirm-execution`を必須とします。
ExperimentSpecの`runner`にある相対`fixture_path`をSpecの親directoryから解決し、sourceを
直接実行せずsystem temporary directory内の使い捨てコピーでGateを実行します。Evidence
はstdout、stderr、終了状態、termination結果、diffを保存します。全commandが通常終了し、
diffの行数が完全で、Workspaceを削除できた場合だけRunMetricsを生成します。通常の非0
終了は品質不合格ですが、signal終了、timeout、起動失敗、output収集失敗、回収失敗、
Evidence不完全はHarness障害であり、Metricsは`null`です。

サンプルRecordingはReplay pipelineを検証するための合成fixtureです。Provider性能の
実験結果ではありません。形式と検証規則は
[docs/REPLAY_FORMAT.md](docs/REPLAY_FORMAT.md)を参照してください。

`experiments/examples/fixtures/runner-smoke`もRunnerとEvidenceを確認するためだけの小さな
合成Fixtureです。Phase 6で計画するmulti-language Task Fixtureではなく、Python
コーディング能力やProvider性能を測定しません。

Phase 2 RunnerはOS security sandboxではありません。process group、環境allowlist、
出力上限、使い捨てコピーによる事故範囲の縮小は行いますが、filesystemの完全隔離、
network遮断、CPU/memory quota、悪意あるprocessの完全な封じ込めは保証しません。
信頼済みのSpec、Fixture、commandだけに使用してください。詳細は
[docs/SAFE_RUNNER.md](docs/SAFE_RUNNER.md)を参照してください。
macOSはlocal process-tree testで検証済みです。Linux実装経路は有効ですが、同等の実機
またはCI検証が完了するまでは「対応設計済み・未検証」です。

## ロードマップ

- Phase 0: Foundation and Capability Spike（完了）
- Phase 1: Replay Vertical Slice（完了）
- Phase 2: Safe Runner, Evidence and Quality Gate（完了）
- Phase 3: Codex CLI Provider（完了）
- Phase 4: Workflow A/B Experiment（完了、Campaign 002は3/3 complete pairs）
- Phase 5: Antigravity CLI Provider（Blocked、Slice 5A offline実装済み、Slice 5B／5Cは
  上流artifact署名検証失敗により停止）
- Phase 6: Multi-language Fixtures and Public Report（完了、Python／Java各1/1 complete pair、
  accepted Public Suite公開済み）
- Phase 7: Optional Enhancements

詳細は [docs/ROADMAP.md](docs/ROADMAP.md) を参照してください。

> 実験結果は本ベンチマーク条件内のものです。一般的なモデル性能を断定するものでは
> ありません。Provider比較はモデル単体ではなく、モデル、利用可能なツール、Agent
> Harnessを含むシステム比較です。
