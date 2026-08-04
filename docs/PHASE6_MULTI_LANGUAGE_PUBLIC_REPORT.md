# Phase 6: Multi-language Workflow Fixtures and Public Report

## Status

**Phase 6: Complete**

- Slice 6A Contract / strict loader: implemented (offline)
- Slice 6B Fixture Acceptance: implemented (local-only; no status transition)
- Slice 6C Plan-bound Campaign execution: implemented; Python／Java Live accepted
- Slice 6D Public Suite renderer and atomic publication: implemented and published
- Slice 6E reviewed Live Campaign and public bundle: complete

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
| Recording | 1.3（current writer）／1.2（accepted Python） | Phase 6 terminal状態とProvider診断metadata |
| Live Evidence／LiveRunArtifact | 1.3（current writer）／1.2（accepted Python） | top-level出力拒否、Gate非実行理由、Plan-bound hash |
| nested Codex Execution Evidence | 1.6（current writer）／1.5（accepted Python） | Provider実行と安全な診断metadata |
| Historical Verification Record | 1.0 | Campaign 002の非再実行検証。toolchainは`unknown`固定 |
| Public Suite Manifest | 1.0 | 明示入力、期待言語status、coverage、cutoff、予定出力 |
| Public Run Record | 1.0 | 公開allowlistだけからなる正規化run |
| Public Language／Suite Report | 1.0／1.1 | 1.0互換読取、call状態付き言語集計、決定的Suite集計 |
| checksums／release metadata／external anchor | 1.0 | bundle integrityと外部固定境界 |

新契約は未知field、型強制、重複JSON key、非有限数を拒否する。JSONはUTF-8、
key sort、2-space indent、末尾newline、時刻のmicrosecond固定を含むcanonical bytesを
要求する。Campaign 1.2とcurrent writerのRecording 1.3はkey sort済みcompact JSONLとする。

後方互換loaderは旧versionを既存loaderへ委譲する。特にPlan 1.1へPlan 1.2のcanonical
制約を遡及しない。Spec 2.0、Plan 1.1、Campaign 1.1、Recording 1.0/1.1/1.2、
LiveRunArtifact 1.0/1.1/1.2の意味とruntime経路は変えない。

accepted Python Campaignは診断schema拡張前のRecording 1.2、Live Evidence 1.2、nested Codex
Evidence 1.5であり、accepted Java `java-independent-004`はcurrent writerと同じRecording 1.3、
Live Evidence 1.3、nested Codex Execution Evidence 1.6である。strict reader／validatorは
両versionをfail-closedで検証し、Public Suite rendererはmixed-version accepted inputsを
正規化Public Run Recordへ変換する。既存Python Artifactはmigrationも再serializationも行わず、
schema version差を評価条件差やbinding欠落として扱わない。未対応versionとunknown fieldは
引き続きfail-closedで拒否する。

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

current writerのLiveRunArtifact 1.3とRecording 1.3は同じGate、diff、Metrics、workspace
lifecycle観測を持つ。後方互換readerで受け入れるaccepted Pythonの1.2 pairにも同じ共通binding
検証を適用し、1.3固有の診断metadataは1.3のstrict規則で検証する。
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

Recording 1.2／1.3はいずれも`Recording started <= Codex started <= Codex completed <= Recording
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
Gate非実行理由は双方向に整合させる。Gate実行済みの場合、Gate集計値はMetricsの有無に
かかわらず保存済みCommand Evidenceから再導出する。Metricsが存在する場合は、そのGate集計と
完全一致させる。

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
destinationを置換せず、途中bundleを残さない。bundle外anchorを含むchecksum契約は整合性を
提供するが、authenticityを保証しない。

## Slice 6D deterministic public renderer

Slice 6Dは`agentlab.phase6_public`へruntimeを分離し、Public schemaを変更せず、次の2 CLIを
追加する。

- `verify-phase6-historical`: 明示されたPlan 1.1、Campaign 1.1、保存Report JSON／Markdownを
  固定rootからstable readする。さらに`--reviewed-spec`でrepository root基準のcanonical相対
  pathを必須入力とし、Specをstable readした同じbytesについてPlanのSpec SHA-256、`git log`、
  `git show`を照合する。filenameやexperiment IDからSpec pathを推測しない。同じbytesの一時
  mirrorでoffline再集計し、CampaignやGateは再実行せず、toolchain versionは`unknown`のまま
  Historical Verification Record 1.0をcreate-onlyで生成する。
- `publish-phase6-public-suite`: `load_public_suite_inputs`と
  `validate_public_suite_inputs`を通過した、Manifest列挙入力だけから決定的bundleを生成する。
  directory探索や未列挙fileの自動採用は行わない。

Public Run Recordは既存1.0 allowlistをfield単位で構築し、Evidenceから1 Provider callを
確認できたrunだけをPlan index pathへ出力する。Prompt本文、raw output、agent message、
reasoning、diff、stdout／stderr、thread／session ID、絶対path、secretは公開しない。0-call、
preflight停止、`input_changed`はRun Recordを作らず、Public Language Report 1.1の
`zero_call_runs`とGate非実行理由にaggregate-onlyで保持する。UsageとMetricsの欠測は0へ
変換しない。

Language Report 1.1とSuite Report 1.0はCampaign terminal eventからの再導出値を使う。
PrimaryとHistoricalの分母は分離し、Historicalにはstrict-load済みVerification Recordだけを
固定sort indexへ複製する。Campaign 002はHistorical候補だが、Slice 6D実装受入では再実行も
Verification Record生成も行わない。Antigravity coverageは
`not_evaluated / upstream_artifact_signature_invalid`に固定する。総合winner、leaderboard、
統計的有意差は生成しない。

JSONはcanonical serializer、Markdownはstrict-load済みJSON modelからの固定rendererで作る。
全出力はUTF-8／LF／末尾newline 1個で、時刻は`data_cutoff_at`だけを使う。stagingでは全fileを
fsyncする。全file生成後とpublish lock取得後にstagingの実treeを安全に再読込し、予定path／
directory集合、通常file、single link、identity、実bytes、JSON strict reload、Markdown byte
再生成、allowlist leak scan、size／SHA-256、`checksums.json`を再検証する。rename前にbundleと
anchorのidentityを保存し、rename成功直後からrollback対象とする。destination／anchor非存在を
確認し、macOSの`renamex_np(RENAME_EXCL)`またはLinuxの
`renameat2(RENAME_NOREPLACE)`だけでpublishする。unsupported platformはfail closedとする。

Slice 6D実装受入時点の受入対象は一時directoryのsynthetic Artifactだけであった。実Historical Verification
Record、実Public Suite bundle、実Codex／Antigravity CLI、Provider call、Prompt送信、Live、
network、quota利用は0件である。Python／Javaは`ready_not_run`、TypeScriptは`not_ready`を
維持した。Slice 6D完了だけではLive-readyでもPhase 6 Completeでもなく、Slice 6Eはレビュー
承認まで開始しないという当時の境界であり、後述のAccepted closeoutがこの状態をsupersedeした。

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
再確認する。Gate前のtoolchain再検証失敗はGate 0件、Gate後の失敗は実行済みCommand Evidenceを
保持し、どちらも明示的なDiff collection errorを伴う`evidence_error / harness_failure`として
canonical Campaignを完結する。cleanup failureは他の結果より優先する。

engineering minimum用PlanはPython／Javaを独立に各1 task、one-shot／staged各1回、1 pair、2
planned Provider calls（合計4 calls）とした。Slice 6C完了時点では両言語を`ready_not_run`とし、
TypeScriptは`typescript_compiler`未解決のため`not_ready`を維持した。実Codex Provider call、
Prompt送信、Live Campaignは0件であった。Slice 6C完了だけではPhase 6はLive-readyでもComplete
でもなく、Public renderer／publisherは後続Sliceとしてレビュー待ちだった。この初期計画と
当時状態は、後述のAccepted closeoutに至る履歴として保持する。

## Accepted closeout

Phase 6の公式status正本は、人間が最終AcceptanceしたPublic Suite release
`phase6-java-evaluated-0e6d894d-001`である。accepted identityは次のとおりである。

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| Public Suite Manifest | 7,548 | `88db41ae59fe03cff87d6d775cdded5dfdf6117bf73db02d36baad535a54b819` |
| `checksums.json` | 2,268 | `43352dc27e7f5ffca63b9bdd65a3e38b100b255241a6240d99905ce0dc21f526` |
| External Checksum Anchor | 259 | `50de193368057c6e095ec7625a31b67d62c5cd90fd997975debe72931422b818` |

新releaseはpublicationだけではcurrent正本にならず、人間による明示的な最終Acceptance後に
current正本となり、旧accepted release `phase6-python-evaluated-8a0aa704-002`をsupersedeした。
旧releaseのManifest（6,204 bytes、SHA-256
`cebd6f88bfc8094456e94c48b232301cc055f0469afec143b5f6ccb48f29a6f3`）を含むArtifactは削除、
上書き、変更せず、不変の監査Artifactとして保持する。複数Manifestが存在してもfilesystem上の
新しさ、名前順、timestamp、HEADだけではcurrentを選択せず、人間がcurrentとして明示Acceptance
したrelease内Manifestをstatus正本とする。repository内へ別のcurrent-selector Artifactは
新設せず、本書とROADMAPがこの人間Acceptanceとcurrent選択をtracked closeout記録として残す。

published bundleは14 filesで、保存入力からのin-memory rendererと14/14でbyte一致する。
`checksums.json`は自身を除く13 filesを保護し、そのSHA-256をbundle外Anchorへ固定する。
Manifest、bundle、Anchorはstrict／canonical validationと人間レビューを通過している。

Primary評価対象は次の2 Campaignである。

| Language | Primary Campaign | Experiment ID | Status | Complete pair |
|---|---|---|---|---:|
| Python | `phase6-python-workflow-independent-001` | `phase6-python-workflow-independent-001` | `evaluated` | 1/1 |
| Java | `java-independent-004` | `phase6-java-workflow` | `evaluated` | 1/1 |

Javaは`staged`／`one_shot`ともProvider成功かつ全Acceptance／Regression／lint／typecheck Gateが
PASSした。Python／Javaとも評価範囲は1 Fixture・1 complete pairである。Public Suiteの
language reportは保存Campaign／Evidenceからrendererが再導出する。`report-workflow`が生成した
Formal Workflow Reportは別のprovenanceを持ち、Manifest inputやPublic bundleへ含めない。

評価分母からは、Pythonのabandoned／inconclusive Campaign、Javaの`java-rebound-001`、
`java-independent-002`、`java-independent-003`、Historical Campaign、Mac Terminalの
health-check callを除外する。これらは削除せず監査Artifactとして保持する。Historical sourceは
`included_in_primary_denominator=false`に固定する。

Provider消費の監査上の累積は`9または10 calls`である。確定下限は9、上限は10で、不確定性は
旧Python Campaignの0または1 callに由来する。このaccountingは評価分母と分離する。

TypeScriptはaccepted Public Suiteに含まれておらず、評価済みとは扱わない。accepted evaluation
scopeはPython／Javaだけであり、追加言語評価はcurrent roadmapからde-scope済みである。将来
必要になった場合は、新しい独立Approval／Spec／releaseとして扱い、Phase 6 Completeには影響
させない。AntigravityはPhase 6の必須Providerではなく、
`not_evaluated / upstream_artifact_signature_invalid`を維持する。Codex agent内のnested
`codex exec`はpermission failureとなったが、詳細なOS-level root causeは未確定である。一方、
Mac Terminalで明示的な絶対`CODEX_HOME`を設定したJava Campaignは成功した。現行運用ではLive
CampaignだけをHost Terminalから実行し、sandbox制約を回避する実装は行わない。offline準備、
validation、Report、publication、監査はCodexから実施できる。承認境界と手順は
[Host Terminal Live Campaign Runbook](HOST_TERMINAL_LIVE_RUNBOOK.md)に固定する。

Phase 6 Completeの最低条件は、最低2言語の実toolchain受入、各言語1 complete pairを持つ
承認済みLive、Public Suite bundle完成である。Python／Javaでこのengineering minimumを満たした。
2言語各3反復の12 callsは初期のprimary publication target、3言語各3反復の18 callsは初期の
full targetであり、いずれもhistorical planであってminimumではない。追加言語は現在の必須作業
ではない。

automatic winner、leaderboard、統計的有意性は生成しない。観測値は当該Fixture、Prompt、Gate、
environment、実行時期へ限定し、一般的なWorkflow、Provider、model性能差へ一般化しない。
cached inputを考慮しない単純なtoken／コスト比較を確定的なコスト差として扱わない。
