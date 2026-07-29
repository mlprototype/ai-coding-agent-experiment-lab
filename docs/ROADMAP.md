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
Workflow優位性を示さない。受入条件を満たしたためPhase 4は`Complete`、Phase 5は
`Current`とする。

## Phase 5: Antigravity CLI Provider

**Status: Current**

**目的:** Antigravity CLIを同じProvider境界へ接続し、Provider比較を可能にする。

**現在の範囲:** 2026-07-28に
[offline設計](ANTIGRAVITY_PROVIDER.md)を確定し、2026-07-29にSlice 5Aのversion付き契約、
strict `stream-json` parser、read-only version/help preflight、redaction済みEvidence、
fake `agy`受入を是正・補完した。Slice 5BはHeadless Runner readinessとoffline統合の
設計だけを確定し、実装は承認していない。実`agy -p`、認証、model catalog、quota利用、
Live、Provider比較も承認していない。公式headless interfaceではPromptがargvへ載るため、
公式ドキュメントにおけるnon-argv transportの存在を調査した。確認できなかったため判定を `not_verified` とし、
[Slice 5B Prompt Transport Decision Record](decisions/SLICE_5B_PROMPT_TRANSPORT_DECISION.md) に基づき Slice 5B 実装は Blocked / Deferred（保留・fail closed）とする。

**実装済み成果物:** Antigravity Evidence 1.0/1.1、構造化version/help preflight、
profile選択境界、event/usage正規化、raw非永続化、process-group回収、合成fake `agy`受入。

**未実装成果物:** Antigravity Headless Runner、Live Recording/Artifact、品質Gateとの
vertical slice、Provider比較Spec・scheduler・report。

**Slice 5A受入条件:** 外部AI、network、認証、quota、実Antigravity Provider callを0件に
保ち、Promptやraw streamを永続化せず、失敗分類を分離し、既存schemaとPhase 0〜4の互換性を
維持する。

## Phase 6: Multi-language Fixtures and Public Report

**Status: Planned**

**目的:** 複数言語・課題へ適用範囲を広げ、再現可能な公開結果を作る。

**成果物:** version固定された複数fixture、fixture検証、集計pipeline、方法・制約・
生データ参照を含む公開レポート。

**受入条件:** 各fixtureが独立したAcceptance/Gateを持ち、初期状態を再生成できる。
レポート数値をrun証跡へ追跡でき、一般的モデル性能を断定しない。

## Phase 7: Optional Enhancements

**Status: Planned**

**目的:** 実験運用で確認された費用対効果に基づき、任意機能を追加する。

**成果物候補:** 可視化、追加Provider、信頼区間、実験catalog、承認workflow、
artifact retention、観測可能な場合のUsage/コスト拡張。

**受入条件:** 採用機能ごとに問題、データ契約、脅威model、保守責任、受入条件を別途
定義する。既存記録のReplay互換性と通常CIのLive禁止を維持する。
