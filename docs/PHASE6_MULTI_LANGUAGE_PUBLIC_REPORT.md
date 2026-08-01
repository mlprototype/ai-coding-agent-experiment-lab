# Phase 6: Multi-language Workflow Fixtures and Public Report

## Status

**Phase 6: Current**

- Slice 6A Contract / strict loader: implemented (offline)
- Slice 6B Fixture Acceptance: implemented (local-only; no status transition)
- Slice 6C Plan-bound Campaign execution: implemented (offline/fake accepted; Live 0)
- Slice 6D Public Suite renderer and atomic publication: not started
- Slice 6E reviewed Live Campaign and public bundle: not started

Phase 6はPhase 4 Completeに依存し、Phase 5には依存しない。Phase 5、Slice 5B、
Slice 5Cは`upstream_artifact_signature_invalid`により`Blocked`のまま維持する。
Phase 6はCodex上の`one_shot`／`staged` Workflow比較だけを対象とし、Provider比較を
行わない。Antigravityは公開coverageで
`not_evaluated / upstream_artifact_signature_invalid`とする。

## Slice 6A scope

Slice 6Aはversion付き契約、strict loader、canonical serialization、Artifact間validator
だけを提供する。Fixture作成、capability audit、Plan生成CLI、Campaign scheduler、
Provider／Gate実行、Public Report生成、bundle publishは含まない。

契約実装は`agentlab.phase6`へ分離し、既存runtimeが使うWorkflow Spec 2.0、Workflow Plan
1.1、Campaign 1.1、Recording 1.0/1.1、LiveRunArtifact 1.0/1.1、
CodexExecutionEvidence 1.5を変更しない。

## Slice 6B Fixture Acceptance

Slice 6BはPython、TypeScript、Javaごとに独立した、外部依存のないTag Normalizer Fixtureを
提供する。3言語で、ASCII入力に限定した同じAcceptance／Regressionケースと次の機能要件を
共有する。

- 前後のASCII空白を除き、ASCII英字をlowercaseへ変換する。
- ASCII空白とunderscoreの連続を1個のhyphenへ変換し、先頭・末尾のhyphenを除く。
- 空文字を除外し、正規化後の重複を最初の出現順で除去する。

各Fixtureで編集可能なのは実装file 1個だけである。Gate helperと設定はprotectedとし、
未分類path、symlink、hardlink、特殊file、許可されない作成／削除を拒否する。TypeScriptの
Workspaceには`node_modules`を置かず、JavaはMaven／Gradleを使わずJDKだけ、Pythonも標準
runtimeだけを使う。lint／typecheckはFixture固有の決定的Gateであり、ESLint、mypy、
Checkstyle等との同等性を主張しない。

`accept-phase6-fixtures`は`--confirm-local-execution`がある場合だけ、固定絶対pathのversion
commandと信頼済みFixture Gateを起動する。確認なしではsubprocessは0件である。baselineと
referenceは別々の使い捨てWorkspaceで実行し、source Fixtureは直接実行しない。baselineは
Acceptanceだけが通常終了の品質FAIL、Regression／lint／typecheckがPASSでなければならない。
隔離されたreference overlayをeditable pathだけへ適用したWorkspaceでは全4 GateのPASSを
要求する。reference solutionはbaseline、Prompt、Provider Workspace、生成Artifactに含めない。
全process group、Workspaceの回収とsource不変を確認し、全条件成功時だけManifest 1.0と
Acceptance Record 1.0を`.artifacts/phase6/fixture-acceptance`配下へcreate-onlyで生成する。
staging directoryからのpublishはmacOSの`renamex_np(RENAME_EXCL)`またはLinuxの
`renameat2(RENAME_NOREPLACE)`を使い、競合して作られた空directoryを含む既存destinationを
atomicに置換せず失敗する。

実Acceptanceのprovenanceはclean worktreeのfull HEADへ固定し、Fixture／Policy／referenceが
そのcommitのtracked fileであることを開始前に検証する。`acceptance_agentlab_commit`と
`fixture_source_commit`は同じHEADである。1言語のblocker後も残りを独立して評価し、2言語を
engineering minimum、3言語をfull targetとする。これはFixture受入条件であり、Slice 6Bだけで
言語statusを`ready_not_run`へ変更せず、Live-readyやPhase 6 Completeも宣言しない。

## Versioned contracts

| Contract | Version | Responsibility |
|---|---:|---|
| Fixture Manifest | 1.0 | 言語、fixture revision、Fixture／Gate hash、実測toolchain |
| Diff Policy | 1.0 | 編集許可／保護pathとlink・特殊file拒否方針 |
| Fixture Acceptance Record | 1.0 | 初期FAIL、reference全Gate PASS、provenance |
| Workflow Experiment Spec | 2.1 | 言語とAcceptance／Policy入力pathをSpec 2.0へ追加 |
| Workflow Plan | 1.2 | Fixture、Acceptance、Policy、Gate、toolchain、commitを事前登録 |
| Campaign | 1.2 | `output_contract_violation`と`input_changed`を含むrun状態 |
| Recording | 1.2 | Phase 6 terminal状態。nested Evidenceは既存1.5のまま |
| LiveRunArtifact | 1.2 | top-level出力拒否、Gate非実行理由、Plan-bound hash |
| Historical Verification Record | 1.0 | Campaign 002の非再実行検証。toolchainは`unknown`固定 |
| Public Suite Manifest | 1.0 | 明示入力、期待言語status、coverage、cutoff、予定出力 |
| Public Run Record | 1.0 | 公開allowlistだけからなる正規化run |
| Public Language／Suite Report | 1.0／1.1 | 1.0互換読取、call状態付き言語集計、決定的Suite集計 |
| checksums／release metadata／external anchor | 1.0 | bundle integrityと外部固定境界 |

新契約は未知field、型強制、重複JSON key、非有限数を拒否する。JSONはUTF-8、
key sort、2-space indent、末尾newline、時刻のmicrosecond固定を含むcanonical bytesを
要求する。Campaign／Recording 1.2はkey sort済みcompact JSONLとする。

後方互換loaderは旧versionを既存loaderへ委譲する。特にPlan 1.1へPlan 1.2のcanonical
制約を遡及しない。Spec 2.0、Plan 1.1、Campaign 1.1、Recording 1.0/1.1、
LiveRunArtifact 1.0/1.1の意味とruntime経路は変えない。

## Plan-bound provenance

Plan 1.2は既存Plan fieldに次を追加する。

- `language`
- `reviewed_commit`
- `fixture_manifest_sha256`
- `fixture_acceptance_sha256`
- `diff_policy_sha256`
- `gate_contract_sha256`
- `reference_solution_sha256`
- `toolchain_fingerprint`

cross-artifact validatorはSpec bytes、Fixture Manifest、Fixture Acceptance Record、
Diff PolicyのSHA-256と意味上の識別子をPlanへ照合する。Acceptanceを実施したAgentLab
commit、SpecとPlanの`reviewed_commit`は完全一致しなければならない。language、
fixture revision、Fixture／Gate／reference solution／toolchainのhashも全契約で一致させる。
reference solutionはProvider Workspaceへ含めない。

Spec 2.1の`fixture_manifest_path`、`fixture_acceptance_path`、`diff_policy_path`はSpecの
親directoryを基準に解決する。Public Suite側の対応するArtifact referenceはSuite rootを
基準に解決し、両者が同一の列挙済みfileを指す場合だけ受理する。

Slice 6B以降では、Fixture Acceptance後のPlan生成時と、各Provider call直前に同じ
validatorを使用し、Plan-bound source bytesが変化していないことを確認する。不一致時は
Plan生成またはProvider callを開始しない。

## Toolchain identity

toolchain versionはcapability auditで得た実測値だけを記録し、未実測のCPython等を仮定しない。
fingerprintはOS、architecture、順序固定したcomponent、Harness固定Gate PATH、
`workspace_executable_lookup_allowed=false`のcanonical JSONに対するSHA-256である。

- PythonはPython runtimeを1 componentとして固定する。
- TypeScriptはNode runtimeとTypeScript compilerを分離する。compilerには解決済み絶対path、
  executable hash、package version、compiler package fingerprintを必須とする。
- Javaは外部依存を持たない`javac`方式とし、Java runtimeとcompilerを分離する。

各version commandは解決済み絶対pathをargv先頭に持つ。Gate PATHはHarness管理の絶対path
だけから構成し、親processのPATHやWorkspace内の同名実行fileを探索しない。version確認にも
timeout、64 KiB stream別出力上限、strict UTF-8、process group回収を適用する。signal、timeout、
非0終了、truncation、decode置換、回収失敗は受け入れない。symlinkはrealpathへ解決し、通常file、
実行権限、link count、実行前後のdevice／inode／mode／size／mtime／ctime／bytes hashを照合する。
Javaの`java`／`javac`は同じJDK rootを必須とする。

Gate contractは記録したtoolchainを実行argvへ直接束縛する。Java Gateは監査済み`java`で
Gate helperを起動し、監査済み`javac`絶対pathをhelperへ渡して対象実装のcompileに使用する。
TypeScript Gateは監査済みNodeでhelperを起動し、同じNodeとpackage内の`lib/tsc.js`を明示して
compilerを起動する。`tsc` launcherのshebangやWorkspace PATHには依存しない。各Gateの直前・
直後に全componentのrealpath、device／inode／mode／link count／size／mtime／ctime／SHA-256を
再確認する。TypeScriptではさらにpackage tree、`package.json`、`lib/tsc.js`とpackage
fingerprintを再計算し、差し替えがあればAcceptanceを拒否する。

version出力hashはstdout／stderrのCRLFとCRをLFへ正規化し、`domain`、exit code、正規化後の
stdout、stderrを持つcanonical JSON bytesのSHA-256とする。完全一致versionは非空のstdout、
stderrの順で各末尾LFを1個だけ除き、両方がある場合はLFで結合するため、stderrへversionを出す
Javaも保持できる。TypeScript compilerの`package_fingerprint`はhash domain
`agentlab-phase6-typescript-package-v2`のcanonical JSONに、package version、`package.json`
hash、package tree hash、`lib/tsc.js` hash、tsc launcher hash、使用するNode component全体、
Node＋compiler JSのversion argvとversion出力hashを含める。auditではlauncherによるversionと
Node＋compiler JSによるversionの完全一致およびstream別出力hash一致を要求する。

Slice 6Bで使うhash対象は次のとおり固定する。

| Field | SHA-256 domain / bytes |
|---|---|
| `fixture_sha256` | domain `agentlab-phase6-tree-v1`、baseline treeの相対directory／file pathとfile bytes |
| `fixture_manifest_sha256` | canonical Fixture Manifest 1.0 bytes |
| `diff_policy_sha256` | canonical Diff Policy 1.0 bytes |
| `gate_contract_sha256` | domain `agentlab-phase6-gate-contract-v1`、Gate順序／argv、Runner上限、固定Gate PATH |
| `reference_solution_sha256` | baselineと同じtree domainによる隔離reference overlayの相対pathとbytes |
| `toolchain_fingerprint` | 既存ToolchainIdentityのcanonical fingerprint |

tree hashはdomain prefixに続き、sort済みentryごとに種別、UTF-8 path長、pathを、fileではさらに
content長とbytesを長さ付きで連結する。Fixture Manifest、Acceptance Record、referenceは
baseline tree外にあり、`fixture_sha256`へ含めない。自己参照hashは作らない。

## Language status

Manifestは言語ごとに`expected_language_status`を持つ。generatorは保存済みCampaignと
strict-load済みEvidenceのrun identityから`derived_language_status`を計算し、完全一致
しなければ生成を拒否する。Public Run Recordはこの判定の入力にしない。

| Status | Derived condition |
|---|---|
| `not_ready` | Plan-bound入力が一つ以上不足 |
| `ready_not_run` | 全入力が揃い、Campaignが存在しない |
| `evaluated` | 同一task／repetitionにEvidence付きのcomplete pairが1組以上存在 |
| `blocked` | blockerあり、またはCampaignはあるがcomplete pairが成立しない |

1言語の`blocked`は他言語の受入・実行を妨げず、Phase 6全体を自動的にBlockedへ変更しない。

## `input_changed`

`input_changed`は品質不合格やProvider失敗ではなくHarness safety stopである。

| Observation | Required value |
|---|---|
| Campaign run status | `not_run` |
| Campaign outcome / stop reason | `stop_condition / input_changed` |
| Provider call count | `0` |
| Gate | 未実行 |
| failure count | 対象外 |
| `fail_fast` / `max_failures` | 対象外 |
| LiveRunArtifact / Recording | 生成しない |
| Public Language Report | `not_run_runs`と`gate_not_executed_reason.input_changed`へ保持 |

Campaign 1.2はevent timestampの非減少順、runごとのstarted→terminal順、
run ID／task／Workflow／repetition identity、Planと全terminal run IDの完全一致を要求する。
`not_run`の理由はCampaign finishedのstop reasonと一致させる。Evidence／Recordingの必要集合は
`completed`／`failed` Campaign状態から導出し、不足、余分、重複を拒否する。

`interrupted`は次の状態に固定する。

| Observation | Required / allowed value |
|---|---|
| status | `interrupted` |
| outcome / stop reason | `human_interruption / human_interruption` |
| Gate | 未実行 |
| Provider call count | 中断位置を確定できれば`0`または`1`、それ以外は`unknown` |
| failure kind | なし |
| failure count | 対象外 |
| `fail_fast` / `max_failures` | 対象外 |

`unknown`を許すのはEvidenceを生成しないこの状態だけである。

Evidenceを持つterminal runのProvider call数はCampaignの自己申告を正本にしない。
CodexExecutionEvidenceの`provider_invocation_attempted`から`1`、preflight段階で停止した
状態から`0`を導出し、run eventとCampaign finishedの合計へ照合する。

## Output contract rejection

`output_contract_violation`はProvider成功後、期待した安全な出力契約を満たさず、Gateを
開始する前に拒否した状態である。LiveRunArtifactは`rejected`、Metricsなし、Recordingは
`run_failed`、Campaignは`failed`かつ通常のfailure count対象とする。後続runは
`fail_fast`／`max_failures`が発動しない場合だけ継続できる。process cleanup失敗を同時に
観測した場合は`process_cleanup_error / cleanup_failure`を優先し、failure count対象外で
停止する。Provider failure、quality gate failure、Harness failureとは別の状態である。

## Manifest, timestamp, and path safety

Public Suite Manifest 1.0はPrimary／Historicalを分離し、入力Artifactの相対pathと
SHA-256、Plan、Campaign、Historical Verification Record、Provider coverage、
Antigravity blocker、renderer version、予定出力、`data_cutoff_at`を明示列挙する。また
`zero_call_run_publication=aggregate_only_no_run_record`を固定し、directory自動探索で
入力を追加しない。

`data_cutoff_at`は列挙されたCampaign terminalとAcceptance／Historical verificationの
timestampの最大値である。Manifest値と再計算値を完全一致させ、UTCのcanonical RFC 3339
`YYYY-MM-DDTHH:MM:SS.ffffffZ`だけを受け入れる。

loaderは呼出側が渡した固定rootを基準とし、Manifest自身と列挙入力だけを読む。absolute
path、`..`、非canonical path、正規化後重複、symlink、hardlink、特殊file、root外参照を
拒否し、各fileのSHA-256をManifestへ照合する。

Manifest自身はresolve前にsymlinkを拒否する。固定rootを`O_NOFOLLOW`相当で開き、そのFDから
各親directoryとfileを相対openする。rootと全親directoryはdevice／inode／mode、Manifestと
各入力fileはdevice／inode／mode／link count／size／mtime／ctimeが読取前後で一致する場合
だけsnapshot化する。strict loaderはhash検証済みsnapshot bytesだけをparseし、pathから
再読込しない。cross-artifact validationの開始時と終了時には全path componentのsymlinkと
identityおよびfile snapshotを再検証し、root／中間directory／fileの差し替えや内容変更を
拒否する。

LiveRunArtifact 1.2とRecording 1.2は同じGate、diff、Metrics、workspace lifecycle観測を持つ。
`passed`／`failed`のquality結果はCodex成功、全Gate commandの正常終了、完全なdiff line
count、`workspace_lifecycle=removed`、MetricsとGate集計の一致を必須とする。`passed`は全
Gate成功かつ`quality_gate_pass=true`、`failed`は通常終了したGateの不合格かつ
`quality_gate_pass=false`に固定する。`cleanup_failed`を観測した場合はquality／Provider
結果より`harness_error / process_cleanup_error`を優先し、逆方向の対応も要求する。

Harness failure kindも同じ共通validatorで観測から双方向に決める。Codex成功後の
`gate_harness_error`にはGate実行と、normally-completedでないcommandを必須とし、全commandが
正常終了した場合は拒否する。`unsupported_platform`はCodex Evidenceのfailed status、
`preflight_completed`、`provider_runtime_precheck`、同一failure kindを必須とする。Workspace、
Codex process、Gate processのいずれかに回収失敗があれば、他の分類より
`process_cleanup_error`を優先する。

`evidence_error`は、Codex Evidence自身の`failure_kind=evidence_error`、またはCodex成功後の
`diff.collection_error`という明示的なEvidence収集失敗のどちらかを必須とする。Provider
failureをtop-levelの`evidence_error`へ付け替えることはできない。複数観測がある場合も
process cleanup失敗を最優先とする。この照合はArtifact／Recordingのstrict modelだけでなく、
Campaignとのcross-artifact validationでも再実行する。

Recording 1.2は`Recording started <= Codex started <= Codex completed <= Recording
terminal`を要求する。

## Public allowlist and checksum trust

Public Run Record 1.0はreviewed commit、experiment／run／task ID、language、exact model ID、
reasoning effort、CLI profile／version、OS／architecture、toolchain／Fixture／Prompt／
Plan／Campaign／Evidence／Recording hash、Workflow、repetition、Gate結果、duration、
diff統計、Usage欠測状態、実行期間だけを許可する。

Prompt本文、raw Provider output、agent message、reasoning、thread ID、絶対path、
認証情報、secretはfieldとして持たない。Usage欠損を0へ変換せず、`missing`と`null`で保つ。
0-call terminal run（`input_changed`とpreflight停止を含む）はPublic Run Recordを生成せず、
言語集計だけに残す。Public Run Recordの`provider_call_count`は`1`に固定する。
`run_metrics_available`はduration／diff統計の完全な有無と一致させる。Gate未実行時は
Acceptance／Regression／lint／typecheck値をすべて0とし、`acceptance_passed`は
`acceptance_total`を超えてはならない。overall status、failure kind、Gate状態、Metrics、
Gate非実行理由は双方向に整合させる。

Public Language Report 1.1は1.0のvalidatorとcanonical loader互換性を維持したまま
`zero_call_runs`と
`provider_call_count_unknown_runs`を持つ。`output_rejected_runs <= failed_runs`、
`gate_not_executed_runs <= scheduled_runs`を必須とし、Campaign 1.2のterminal eventから
run taxonomy、Provider call状態、Gate非実行理由、scheduled／complete pairを再導出して
report値と完全一致させる。この2つの上限検証は1.1だけに適用する。reportの`language`は
Primary sourceとPlanのlanguage、`status`はManifest期待値と一致を確認したderived language
statusへ完全一致させる。

`checksums.json`は`release-metadata.json`を含む全予定出力を対象とし、自身だけを対象外と
する。`checksums.json`自身のSHA-256はbundle外のExternal Checksum Anchor 1.0へ固定する。
外部固定前後ともこの契約だけからauthenticityは主張しない。

Slice 6Dのpublisherは全fileを専用staging directoryで作成・strict再読込し、最終file作成後に
staging directoryを再`fsync`する。その後、排他的publish lock下でdestination非存在を
確認し、no-replace相当の方式で1回だけpublishし、親directoryを`fsync`する。既存
destinationを置換せず、途中bundleを残さない。このpublication処理はSlice 6Aでは未実装で
ある。

## Slice 6C Plan-bound Campaign execution

Slice 6CはWorkflow Spec 2.1とcanonical Workflow Plan 1.2をGit管理外へcreate-onlyで準備する。
Plan生成時にFixture、Prompt、Manifest、Acceptance、Policy、Gate、reference、toolchain、reviewed
commitを固定し、Campaign開始前と各Provider call直前に再読込してdriftを検出する。実行には開始時
のin-memory snapshotだけを使い、drift時は`input_changed`、Provider call 0、Gate 0件で
canonical Campaignを完結する。

Provider process groupの回収確認後、Gate起動前にDiff Policyを適用する。protected／未分類path、
禁止された作成・削除、link、特殊file、不完全なsnapshotは`output_contract_violation`として
拒否し、Gateは0件とする。Policy PASS時だけAcceptance、Regression、lint、typecheckを固定
toolchainと固定PATHで実行し、各command前後のtoolchain identity、Gate後Workspace、cleanupを
再確認する。cleanup failureは他の結果より優先する。

engineering minimum用PlanはPython／Javaを独立に各1 task、one-shot／staged各1回、1 pair、2
planned Provider calls（合計4 calls）とする。Plan生成成功後は両言語を`ready_not_run`とし、
TypeScriptは`typescript_compiler`未解決のため`not_ready`を維持する。実Codex Provider call、
Prompt送信、Live Campaignは0件である。Slice 6C完了だけではPhase 6はLive-readyでもCompleteでも
ない。Public renderer／publisherはSlice 6Dであり、レビュー承認まで開始しない。

## Current acceptance boundary

Slice 6Cのoffline/fake受入完了だけではLive-readyでもPhase 6 Completeでもない。実Codex／
Antigravity CLI、Provider call、Prompt送信、network、quota利用、Live Campaign、Public bundle生成
は0件である。次はSlice 6Cレビューであり、Slice 6Dは承認まで開始しない。

Python、TypeScript、Javaは実装目標だが、Live-readyは最低2言語の実toolchain受入完了、
Phase 6 Completeは最低2言語で各1 complete pairとPublic Suite bundle完成を条件とする。
4 Provider callsはengineering minimum、2言語各3反復の12 callsはprimary publication
target、3言語各3反復の18 callsはfull targetである。4 callsだけから傾向、優位性、
再現性を主張しない。
