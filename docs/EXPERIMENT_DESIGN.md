# Experiment Design

## 基本原則

一つのExperimentSpecが変える比較軸は一つだけとする。比較軸以外の条件を固定し、
観測差を独立変数へ帰属できる範囲を明確にする。Specの`control`と`treatments`は
`comparison_axis`が示す軸の値だけを取る。

## 独立変数

- Workflow実験: `one_shot` または `staged`
- Provider実験: Codex CLI、Google Antigravity CLI、Replay Provider

WorkflowとProviderを同じSpecで同時に変更しない。複合効果を調べる場合も、まず単一軸
実験を完了し、その後に別設計として交互作用を検討する。

## 従属変数

- 品質Gate合否、Acceptance Test合格数、Regression失敗数
- lint/typecheckエラー数
- Agent、評価、全体の所要時間
- Agent呼出回数、retry回数
- 変更ファイル、追加・削除行数
- 取得可能な場合だけToken、推定APIコスト、quota消費

行数や時間は品質の代替指標ではなく、品質結果と併せて解釈する。

## 固定する条件

- Task Fixtureの内容とrevision
- 初期リポジトリ状態、依存関係、言語/ツールchain
- Acceptance、Regression、lint、typecheckの各コマンド
- Prompt templateとProviderに渡す要件
- Sandbox、ネットワーク、権限、timeout、停止条件
- Provider実験ではWorkflow、Workflow実験ではProvider
- 反復数と実験単位の定義

モデル版を固定できないProviderでは、実行時に観測できた識別情報と時刻を証跡へ残す。

## Workflow実験

同じProvider、exact model ID、reasoning effort、課題、初期状態、品質Gate、sandbox、
network設定、timeoutを使う。`one_shot`は共有Task要件を一度に渡し、詳細な作業順序を
指定しない。`staged`は同じTask要件へ、調査、計画、テストの確認・追加、実装、
自己レビューと必要な修正という固定手順だけを加える。

両条件とも`1 run = 1 Provider turn = 1 agent call`である。`staged`の段階は一つのPromptと
Provider turn内の論理段階で、複数process、複数turn、session resumeではない。段階間で
人間の追加入力を受けない。内部の調査、計画、reasoning、agent message、stage出力は
永続化せず、最終Workspace変更と既存の正規化Evidenceだけを評価する。Workflowごとに
Provider、model、Fixture、Gate、sandbox、network、timeoutを変える設定は持たない。

Campaign開始時に共有Task Prompt bytesとFixture全file bytesを一度だけsnapshotし、両条件の
全runを同じ固定入力から構築する。run間でsource PromptまたはFixtureが変わった場合は、
次のProvider call前に検出してCampaignを停止し、変更後の入力をCampaignへ取り込まない。

## Provider実験

Workflow、課題、要件、品質Gate、実行環境を固定する。ただしProvider固有のPrompt
変換やHarness差は完全には除去できない。したがってProvider比較はモデル単体の比較では
なく、**モデル・利用可能なツール・Agent Harnessを含むシステム比較**である。結果を
基盤モデル固有の能力へ直接帰属しない。

Replay ProviderはLive Providerと性能を競う対象ではない。Runnerと評価処理を決定論的
に検証するための実行源であり、比較に含める場合は目的を明記する。

## 再現性

- Spec、Fixture revision、Prompt revision、品質Gate、Provider能力、実行環境を記録する。
- Live実行は必ず入力と許可された出力をRecordし、秘密情報を除去する。
- 永続データにはschema versionを付け、移行方針なしに意味を変更しない。
- Replayは記録済みイベントを読み、外部AIサービスを呼ばない。
- 乱数を使う処理にはSpecの`random_seed`から派生したseedを使う。

## 実験順序のランダム化

時間帯、rate limit、Provider側更新、host負荷の偏りを抑えるため、control/treatmentの
実行順をseed付きでランダム化する。単純にcontrolを全件実行してからtreatmentを実行
しない。課題ごとのblock内ランダム化を基本とし、生成された順序を証跡として保存する。

Phase 0は順序生成を実装せず、seedのデータ契約だけを定義する。

## 反復実行

非決定性を単発結果で評価しない。課題と条件の組ごとに同じ反復数を設定し、各runを独立
記録する。欠測、infra失敗、実験失敗を区別し、失敗runを黙って置換しない。反復数は
事前に決め、都合のよい結果が出た時点で停止しない。Stop conditionが発動した場合は
理由と未実行runを残す。

## 結果の解釈上の制約

- 結論の範囲は採用した課題、Prompt、Gate、時期、環境に限定する。
- 統計的不確実性と実務上の効果量を併記し、合否率だけで優劣を断定しない。
- CLIやモデルの更新は処置内容を変えうるため、異なる期間のrunを無条件に統合しない。
- Usage欠損を0とみなさない。推定値とProvider報告値を混在させない。
- Harness障害とAgentによる課題失敗を分離する。
- 本ベンチマーク結果から一般的なモデル性能を断定しない。
