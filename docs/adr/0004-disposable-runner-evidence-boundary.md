# ADR 0004: 使い捨てWorkspaceと実行EvidenceをProviderから分離する

- Status: Accepted
- Date: 2026-07-25

## Context

品質GateをFixture source上で直接実行すると、評価対象を変更し、再現性を失う。shell
commandや親環境の無条件継承は、Spec外の処理、対話待ち、secret漏えいを招く。さらに、
通常の非0終了とtimeout、spawn、process回収、diff収集の失敗を同じ「Gate失敗」にすると、
ProviderやWorkflowの品質をHarness障害として誤評価する。

Phase 2はOS security sandboxを導入しないため、実行境界と保証できない隔離も明示する
必要がある。

## Decision

信頼済みSpecとFixtureだけを対象とし、Fixtureは検証後にsystem temporary directoryの
使い捨てWorkspaceへコピーする。Gate executorはSpecにargv配列で列挙されたcommandだけを
`shell=False`で順番に起動する。

各commandをPOSIXの新しいsession/process groupで起動し、timeout時はSIGTERM、grace、
SIGKILLの順に停止する。正常終了時も残存groupを確認してbackground childを回収する。
必要なAPIがないplatformは起動前にfail closedする。

親環境はallowlist化し、HOME/cache/tempをrun専用pathへ差し替える。stdout、stderr、
diffは別々の設定上限で収集する。

Provider/Workflow結果であるRunMetricsと、Harnessが観測したEvidenceを分離する。
commandが通常終了し、process回収、diff行数、Workspace cleanupが完全な場合だけMetricsを
生成する。timeout、spawn、回収、unsupported platform、Evidence不完全はfailure kindを
持つHarness障害としてMetricsを`null`にする。

## Consequences

- Fixture sourceと実行後Workspaceを比較でき、sourceを評価中に変更しない。
- 品質不合格とHarness障害を分析で分離できる。
- Evidenceからcommand単位Metricsを追跡できる。
- 明示確認、入力alias保護、atomic writerが必要になる。
- process groupと環境allowlistは事故範囲を縮小するが、network、filesystem、resourceの
  OS-level隔離にはならない。
- Windowsやprocess group保証のないOSではPhase 2 Gateを実行できない。
- 悪意あるcodeの実行には将来別のsandbox設計が必要である。
