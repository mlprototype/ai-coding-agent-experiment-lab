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

**Status: Current**

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

## Phase 5: Antigravity CLI Provider

**Status: Planned**

**目的:** Antigravity CLIを同じProvider境界へ接続し、Provider比較を可能にする。

**成果物:** Antigravity adapter、能力/版preflight、event正規化、Record/Replay、
Provider比較Spec。

**受入条件:** Phase 3と同じ安全・証跡基準を満たす。Workflowを固定した反復比較ができ、
Provider固有欠測を明示する。モデル単体比較と表現しない。

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
