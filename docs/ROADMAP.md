# Roadmap

各Phaseは直前Phaseの受入条件を満たしてから開始する。将来Phaseの名称は方向性であり、
Phase開始時に改めて範囲と受入条件を確定する。

## Phase 0: Foundation and Capability Spike

**目的:** 実験基盤の責務、データ契約、Provider境界、実験原則を確定し、ローカルCLI
能力を安全に調査する。

**成果物:** Pythonパッケージ、ExperimentSpec/RunMetrics/UsageMetrics/
CapabilityReport、`doctor`、`validate`、サンプルSpec、設計文書、ADR、テスト。

**受入条件:** 単一比較軸、正の反復数、任意Usage指標、明示的実行モードがテストされ、
CLI未導入でもdoctorが成功する。pytest、Ruff、mypyと必須CLIを実行可能な範囲で通す。

## Phase 1: Replay Vertical Slice

**目的:** 外部AIを呼ばず、一つの記録を一つのRunResultへ変換する最小縦断経路を作る。

**成果物:** Replay記録形式、Replay Provider、run orchestratorの最小版、結果永続化、
単一の小さなReplay fixture。

**受入条件:** 固定記録から決定論的なRunResultを生成でき、ネットワークなしで統合テスト
が通る。破損記録とschema不一致を理由付きで拒否する。

## Phase 2: Safe Runner, Evidence and Quality Gate

**目的:** Fixtureを隔離して実行し、変更と品質Gateの証跡を安全に収集する。

**成果物:** timeout/停止処理を持つRunner、作業領域管理、Gate executor、stdout/stderr・
diff・終了状態のEvidence、失敗分類。

**受入条件:** 許可されたargvだけを`shell=True`なしで実行し、timeout後も子processを
残さない。Gate結果からRunMetricsを再構成でき、通常テストはReplayだけを使う。

## Phase 3: Codex CLI Provider

**目的:** Codex CLIを共通Provider境界へ接続し、安全なLive Recordを実現する。

**成果物:** Codex adapter、能力/版のpreflight、Prompt入力、構造化event変換、
redaction付きrecording、明示的Live opt-in。

**受入条件:** 手動の隔離環境で一つのfixtureを実行・Recordでき、その記録をReplayして
同じ正規化eventを得る。認証情報や機密Promptを成果物へ保存しない。通常CIは呼ばない。

## Phase 4: Workflow A/B Experiment

**目的:** 同一Provider上で`one_shot`と`staged`を比較する最初の反復実験を行う。

**成果物:** 二つのWorkflow定義、seed付き実行順生成、反復scheduler、事前登録Spec、
内部分析notebookまたは集計データ。

**受入条件:** Provider、fixture、Gateを固定し、順序と全runの状態を記録する。欠測と
停止条件を説明でき、結論をベンチマーク範囲に限定する。

## Phase 5: Antigravity CLI Provider

**目的:** Antigravity CLIを同じProvider境界へ接続し、Provider比較を可能にする。

**成果物:** Antigravity adapter、能力/版preflight、event正規化、Record/Replay、
Provider比較Spec。

**受入条件:** Phase 3と同じ安全・証跡基準を満たす。Workflowを固定した反復比較ができ、
Provider固有欠測を明示する。モデル単体比較と表現しない。

## Phase 6: Multi-language Fixtures and Public Report

**目的:** 複数言語・課題へ適用範囲を広げ、再現可能な公開結果を作る。

**成果物:** version固定された複数fixture、fixture検証、集計pipeline、方法・制約・
生データ参照を含む公開レポート。

**受入条件:** 各fixtureが独立したAcceptance/Gateを持ち、初期状態を再生成できる。
レポート数値をrun証跡へ追跡でき、一般的モデル性能を断定しない。

## Phase 7: Optional Enhancements

**目的:** 実験運用で確認された費用対効果に基づき、任意機能を追加する。

**成果物候補:** 可視化、追加Provider、信頼区間、実験catalog、承認workflow、
artifact retention、観測可能な場合のUsage/コスト拡張。

**受入条件:** 採用機能ごとに問題、データ契約、脅威model、保守責任、受入条件を別途
定義する。既存記録のReplay互換性と通常CIのLive禁止を維持する。

