# Roadmap

各Phaseは直前Phaseの受入条件を満たしてから開始する。将来Phaseの名称は方向性であり、
Phase開始時に改めて範囲と受入条件を確定する。

## Phase 0: Foundation and Capability Spike

**Status: Complete**

**目的:** 実験基盤の責務、データ契約、Provider境界、実験原則を確定し、ローカルCLI
能力を安全に調査する。

**成果物:** Pythonパッケージ、ExperimentSpec/RunMetrics/UsageMetrics/
CapabilityReport、`doctor`、`validate`、サンプルSpec、設計文書、ADR、テスト。

**受入条件:** 単一比較軸、正の反復数、任意Usage指標、明示的実行モードがテストされ、
CLI未導入でもdoctorが成功する。pytest、Ruff、mypyと必須CLIを実行可能な範囲で通す。

## Phase 1: Replay Vertical Slice

**Status: Complete**

**目的:** 外部AIを呼ばず、一つの記録を一つのRunResultへ変換する最小縦断経路を作る。

**成果物:** 2イベントのversion付きJSONL契約、厳密なRecording loader、最小Replay
Provider、Spec照合Orchestrator、atomicな結果永続化、`agentlab replay`、単一の合成
Recording。

**受入条件:** 固定Recordingから外部AI、network、CLI、Gate commandを呼ばず、完了時刻と
保存済みMetricsを使って決定論的なRunResultを生成する。同一入力のJSONはbyte単位で一致
する。破損Recording、schema不一致、Spec不一致、暗黙上書きを理由付きで拒否する。

## Phase 2: Safe Runner, Evidence and Quality Gate

**Status: Complete**

**目的:** 信頼済みFixtureを使い捨てWorkspaceで実行し、変更と品質Gateの証跡を
Harness障害と品質不合格を混同せず収集する。

**成果物:** 任意のRunner設定を持つ後方互換ExperimentSpec、Fixture検証と使い捨て
Workspace、POSIX process groupのtimeout/終了時停止、環境allowlist、上限付き
stdout/stderr、標準library snapshot/diff、version付きEvidence、command単位RunMetrics、
`agentlab run-gates`、合成Runner smoke Fixture。

**受入条件:** 明示確認後にSpec記載argvだけを`shell=True`なしで使い捨てコピー上から
実行し、timeout時と正常終了時の残存子processを回収する。秘密環境を継承せず、出力と
diffを上限付きEvidenceへatomic保存する。通常の非0終了だけを品質不合格としてMetricsへ
変換し、signal終了、timeout、spawn、output収集、回収、unsupported platform、
Evidence不完全ではMetricsを生成しない。Phase 1 Replayのbyte決定性と入力保護を維持する。

## Phase 3: Codex CLI Provider

**Status: Complete**

**目的:** Codex CLIを共通Provider境界へ接続し、安全なLive Recordを実現する。

**成果物:** Codex adapter、能力/版のread-only preflight、stdin Prompt、incremental
JSONL parser、Provider process tree停止、正規化Codex Evidence、redaction済みRecording
1.1、Live Evidence、成功RecordingのReplay、明示的Live opt-in、strict paired成果物を
構築できない場合の独立Failure Diagnostic、fake CLI offline test。

**受入条件:** 手動の隔離環境で一つのfixtureを実行・Recordでき、その記録をReplayして
同じ正規化結果を得る。認証情報や機密Promptを成果物へ保存せず、通常CIからLiveを呼ばない。
manual Liveは累計8試行である。006はProvider起動後の`model_access`境界、007は
`inconclusive_prompt_delivery_failure`として履歴を保持し、成功へ変更しない。008は人間が
selectable catalogから明示選択した`gpt-5.6-sol`をcommit
`cc97e53bf0bac426b08346f63e6f527ed7d5be9e`のAgentLab製品経路で1回実行した。agent call 1、
retry／fallback 0、Provider exit 0、`turn.completed` 1件、`turn.failed` 0件で、
`task.txt`だけを期待どおり変更した。4種類のQuality Gateは全PASSし、Evidence 1.5／
Recording 1.1のstrict再読込、offline Replay、Metrics一致、redaction、process group／
Workspace cleanupを確認した。これによりPhase 3の最小vertical slice受入を完了した。
この結果は一般的なモデル性能やProvider比較を示さない。

## Phase 4: Workflow A/B Experiment

**Status: Complete**

**目的:** 同一Provider上で`one_shot`と`staged`を比較する最初の反復実験を行う。

**成果物:** 二つの単一turn Workflow定義、後方互換なSpec 2.0、seed付きblock順序、
byte決定的なcanonical Plan、逐次scheduler、append-only Campaign、JSON/Markdown集計。

**受入条件:** offline実装とfake Codex受入は完了した。Provider、exact model、fixture、
Gate、sandbox、network、timeoutを固定し、両Workflowとも1 run = 1 Provider turn =
1 agent callとする。順序と全run状態を記録し、欠測と停止条件を説明し、結論をFixture、
Prompt、Gate、環境、実行時期へ限定する。Completeには、別の人間の明示指示により
レビュー済みcommitへ事前登録した1 Task×2 Workflow×各3反復（最大6 Provider calls）の
実Codex Campaignを実行し、最低1組のpaired結果をoffline集計する必要がある。安全停止で
pairが成立しない場合はCurrentのままとし、失敗runを再実行しない。

2026-07-28T09:26:35Zに、事前登録commit
`2abd653a7b42f8932c0005e6d7d3fdd1252845e0`、canonical Plan SHA-256
`9caf1847677adfcd6ef7aac59b2298bfbc9113577d75e49a7976de0b068e19de`のLive Campaignを
1回だけ開始した。予定6 run／6 Provider callsに対し、Plan先頭の`staged` runだけを
attemptし、actual Provider callは1、call count unknownは0だった。Provider turnはexit 0、
`turn_completed`で、4種類のGateも各1件PASSした。一方、lintの`py_compile`が作成した
`__pycache__/tag_normalizer.cpython-313.pyc`をbinary diffとして検出し、行数Evidenceを
完全に構築できなかったため、runは`harness_error`／`evidence_error`となった。Provider
process group、Workspace、adapterはいずれも回収済みである。

Campaignは`harness_failure`で停止し、残り5 runを`not_run`として記録した。retry、
fallback、resumeは0で、offline reportは1回だけ生成した。scheduled pair 3に対しcomplete
pairは0、`pairing.status=not_estimable`である。ArtifactはGit管理外で保持し、このCampaignや
失敗run、reportを再実行しない。比較可能なpairがないためWorkflowの優劣を述べず、この
Campaign 001時点ではPhase 4をCurrent、Phase 5をPlannedのままとした。Phase 4の
1 Provider callはPhase 3 manual Live累計8試行へ加算しない。

Python bytecode cache隔離修正後、reviewed commit
`edd8c9e748998d056efa70fa43a26d10aa8ded12`、canonical Plan SHA-256
`375675a105b3de6b371551ab09c25014e3198d256bf09717e80fe20e747125ee`のCampaign 002を、
2026-07-28T11:00:21.701522Zから11:05:11.761906Zまで1回だけ実行した。planned／attempted／
completed／failed／not_runは6／6／6／0／0、actual Provider callsは6、unknown callsは0、
retry／fallback／resumeは0、Campaign outcomeは全run `success`、stop reasonは`none`である。
`one_shot`／`staged`は各3 runを完了し、acceptance／regression／lint／typecheckは各6件
すべてPASSした。scheduled／complete pairは3／3、`pairing.status=estimable`である。
report JSON／Markdown SHA-256は
`d819eb5a1403f623527dcf84c665e88f3ae0b49d6b0878d5dd9941c1f60f139a`／
`936a44a9710d1dbd16ec815a97fac190f3bad4366b27b8b02cf11f7bc5d4af4a`である。
Plan、Campaign、Recording、Evidence、reportはGit管理外に保持する。Campaign 001は
`harness_failure`、complete pair 0、`not_estimable`のまま不変で、再実行、resume、report
再生成を行っていない。Campaign 002も再実行しない。Phase 3 manual Live累計8試行は変更せず、
Phase 4 Campaignのcall数と分離する。この固定Task、Prompt、Fixture、Gate、各3反復、
当該環境・実行時期に限定された結果であり、一般的なモデル性能、統計的有意差、普遍的な
Workflow優位性を示さない。受入条件を満たしたためPhase 4は`Complete`である。Phase 5は
後述する上流artifact受入blockerにより`Blocked`とする。

## Phase 5: Antigravity CLI Provider

**Status: Blocked**

**目的:** Antigravity CLIを同じProvider境界へ接続し、Provider比較を可能にする。

**現在の範囲:** 2026-07-28に
[offline設計](ANTIGRAVITY_PROVIDER.md)を確定し、2026-07-29にSlice 5Aのversion付き契約、
strict `stream-json` parser、read-only version/help preflight、redaction済みEvidence、
fake `agy`受入を是正・補完した。2026-07-30に公式Antigravity CLI 1.1.8
`darwin_arm64` payloadを実行前に受入検証した。manifest記載SHA-512とpayload実測SHA-512は
完全一致し、archiveは単一の通常fileで危険なentryを含まず、Mach-O arm64であることを
確認した。一方、embedded signatureが存在するにもかかわらず、
`codesign --verify --deep --strict`はexit code 1と
`invalid signature (code or signature have been modified)`で失敗した。改ざんや悪意は
断定せず、原因未確定の上流署名工程、archive packaging、または配布artifactの不具合を
含み得る外部blocker `upstream_artifact_signature_invalid`として扱う。binaryは配置も
実行もしておらず、実装不具合やローカルRunner障害による停止ではない。

| Scope | Status | Reason |
|---|---|---|
| Phase 5 | Blocked | Upstream macOS artifact signature verification failed |
| Slice 5B | Blocked | CLI binary was rejected before execution |
| Slice 5C | Blocked | Live execution prerequisites were not satisfied |

公式Changelogにstdin経由のpiped Promptへの言及はあるが、明確な実行構文や制約は
確定できていないため、stdin transportも`not_verified`のままとする。
[Slice 5B Prompt Transport Decision Record](decisions/SLICE_5B_PROMPT_TRANSPORT_DECISION.md)
のfail-closed判断は維持する。実`agy`、認証、Provider call、Prompt送信、quota利用、
Live、Provider比較は0件である。

**実装済み成果物:** Antigravity Evidence 1.0/1.1、構造化version/help preflight、
profile選択境界、event/usage正規化、raw非永続化、process-group回収、合成fake `agy`受入。

**未実装成果物:** Antigravity Headless Runner、Live Recording/Artifact、品質Gateとの
vertical slice、Provider比較Spec・scheduler・report。

**Slice 5A受入条件:** 外部AI、network、認証、quota、実Antigravity Provider callを0件に
保ち、Promptやraw streamを永続化せず、失敗分類を分離し、既存schemaとPhase 0〜4の互換性を
維持する。

**再開条件:** 上流から説明が提供されただけでは再開しない。新しい公式payload、または
公式修正手順を適用したartifactを取得し、checksum、archive安全性、platform、
architectureを再検証したうえで、ローカル環境の
`codesign --verify --deep --strict`が成功することを必須とする。その後も、別途承認された
手順でのみ配置とread-only Preflightへ進む。stdin transportを確認できるまではLiveへ
進まない。当面は上流の修正版または公式修正手順を待ち、必要に応じて再現情報を上流へ
報告する。

## Phase 6: Multi-language Fixtures and Public Report

**Status: Current**

**目的:** Phase 4 Completeを基礎として、Codex Provider上のWorkflow比較を複数言語へ
広げ、保存済みArtifactから追跡可能な公開結果を作る。Phase 5には依存せず、同Phaseの
Blocked状態を変更しない。

**現在の範囲:** Slice 6Aとして、Fixture Manifest／Acceptance／Diff Policy 1.0、
Workflow Spec 2.1、Plan／Campaign／Recording／LiveRunArtifact 1.2、Public Suite
Manifest／Run Record／Report 1.0、canonical serialization、後方互換loader、
cross-artifact validatorをoffline実装した。Slice 6Bでは3言語の独立したTag Normalizer
Fixture、local capability audit、baseline／reference Acceptance、create-only Record生成を
実装した。実測Acceptanceはcleanな実装commitへ束縛し、結果をGit管理外へ保存する。
Slice 6CではSpec 2.1／Plan 1.2の決定的なcreate-only準備、Plan-bound入力のCampaign開始前・
各call直前検証、Provider後・Gate前Diff enforcement、Campaign／Recording／LiveRunArtifact
1.2 runtimeを実装した。`output_contract_violation`ではGateを0件に保ち、Gate後のWorkspaceと
toolchainも再検証する。Python／Javaは各1 pair／2 planned calls、TypeScriptは`not_ready`の
ままとし、実Provider／Prompt／Liveは0件である。Slice 6DではManifestに明示列挙された保存済み
ArtifactだけからPublic Run Record、Language／Suite JSON／Markdown、coverage、checksum、
bundle外anchorを決定的に生成し、stagingからatomic no-replaceでcreate-only publishする
offline runtimeを実装した。受入はsynthetic Artifactだけであり、実Campaign 002 Historical
Verification Recordと実Public Suite bundleは0件のまま、レビュー待ちである。詳細は
[Phase 6詳細設計](PHASE6_MULTI_LANGUAGE_PUBLIC_REPORT.md)を参照する。

**受入条件:** Live-readyには最低2言語の実toolchain Fixture受入が必要で、offline実装だけ
では到達しない。Completeには最低2言語で各1 complete pairを含む承認済みLiveとPublic
Suite bundleが必要である。未実行言語は`not_run`として示す。3言語・各3反復の18 callsは
目標であり、最低条件ではない。レポート数値を正規化run証跡へ追跡でき、総合winner、
leaderboard、統計的有意差、一般的モデル性能を主張しない。

## Phase 7: Optional Enhancements

**Status: Planned**

**目的:** 実験運用で確認された費用対効果に基づき、任意機能を追加する。

**成果物候補:** 可視化、追加Provider、信頼区間、実験catalog、承認workflow、
artifact retention、観測可能な場合のUsage/コスト拡張。

**受入条件:** 採用機能ごとに問題、データ契約、脅威model、保守責任、受入条件を別途
定義する。既存記録のReplay互換性と通常CIのLive禁止を維持する。
