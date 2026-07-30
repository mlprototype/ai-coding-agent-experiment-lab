# Phase 6: Multi-language Workflow Fixtures and Public Report

## Status

**Phase 6: Current**

- Slice 6A Contract / strict loader: implemented (offline)
- Slice 6B Fixture Acceptance: not started
- Slice 6C language-specific Campaign integration: not started
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
| Public Language／Suite Report | 1.0 | 言語別集計と決定的Suite集計 |
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
だけから構成し、Workspace内の同名実行fileを探索しない。実toolchain受入はSlice 6Bであり、
Slice 6A完了だけではいずれの言語もLive-readyにならない。

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
Antigravity blocker、renderer version、予定出力、`data_cutoff_at`を明示列挙する。
directory自動探索で入力を追加しない。

`data_cutoff_at`は列挙されたCampaign terminalとAcceptance／Historical verificationの
timestampの最大値である。Manifest値と再計算値を完全一致させ、UTCのcanonical RFC 3339
`YYYY-MM-DDTHH:MM:SS.ffffffZ`だけを受け入れる。

loaderは呼出側が渡した固定rootを基準とし、Manifest自身と列挙入力だけを読む。absolute
path、`..`、非canonical path、正規化後重複、symlink、hardlink、特殊file、root外参照を
拒否し、各fileのSHA-256をManifestへ照合する。

Manifest自身はresolve前にsymlinkを拒否する。Manifestと各入力はopen前、descriptor読取前後、
close後のdevice／inode／mode／link count／size／mtime／ctimeが一致する場合だけsnapshot化
する。strict loaderはhash検証済みsnapshot bytesだけをparseし、pathから再読込しない。
cross-artifact validationの開始時と終了時に同一pathを再検証し、Manifest load後の差し替えや
内容変更を拒否する。

## Public allowlist and checksum trust

Public Run Record 1.0はreviewed commit、experiment／run／task ID、language、exact model ID、
reasoning effort、CLI profile／version、OS／architecture、toolchain／Fixture／Prompt／
Plan／Campaign／Evidence／Recording hash、Workflow、repetition、Gate結果、duration、
diff統計、Usage欠測状態、実行期間だけを許可する。

Prompt本文、raw Provider output、agent message、reasoning、thread ID、絶対path、
認証情報、secretはfieldとして持たない。Usage欠損を0へ変換せず、`missing`と`null`で保つ。
`input_changed`はProvider callがないためPublic Run Recordを生成せず、言語集計だけに残す。
`run_metrics_available`はduration／diff統計の完全な有無と一致させる。Gate未実行時は
Acceptance／Regression／lint／typecheck値をすべて0とし、`acceptance_passed`は
`acceptance_total`を超えてはならない。overall status、failure kind、Gate状態、Metrics、
Gate非実行理由は双方向に整合させる。

`checksums.json`は`release-metadata.json`を含む全予定出力を対象とし、自身だけを対象外と
する。`checksums.json`自身のSHA-256はbundle外のExternal Checksum Anchor 1.0へ固定する。
外部固定前後ともこの契約だけからauthenticityは主張しない。

Slice 6Dのpublisherは全fileを専用staging directoryで作成・strict再読込し、最終file作成後に
staging directoryを再`fsync`する。その後、排他的publish lock下でdestination非存在を
確認し、no-replace相当の方式で1回だけpublishし、親directoryを`fsync`する。既存
destinationを置換せず、途中bundleを残さない。このpublication処理はSlice 6Aでは未実装で
ある。

## Current acceptance boundary

Slice 6Aの完了はoffline contractとunit testの完了だけを意味する。言語statusは現時点で
`not_ready`であり、Live-readyでもPhase 6 Completeでもない。Slice 6B以降、Codex／
Antigravity CLI、Provider call、Prompt送信、quota利用、Live Artifact、Public bundleの
生成は別承認まで開始しない。

Python、TypeScript、Javaは実装目標だが、Live-readyは最低2言語の実toolchain受入完了、
Phase 6 Completeは最低2言語で各1 complete pairとPublic Suite bundle完成を条件とする。
4 Provider callsはengineering minimum、2言語各3反復の12 callsはprimary publication
target、3言語各3反復の18 callsはfull targetである。4 callsだけから傾向、優位性、
再現性を主張しない。
