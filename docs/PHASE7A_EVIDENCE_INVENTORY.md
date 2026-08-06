# Phase 7A Evidence Inventory — Authority Decision

## Decision

Phase 7AのEvidence Inventoryは、Phase 6の既存Artifactと人間がレビューしたRequestを検証して表示する非権威的な派生成果物である。`authoritative=false`を変更せず、Phase 6のstatus、current、supersession、Artifact本文を更新しない。

## Fixed boundaries

- Phase 6 Artifactのrole-aware `artifact_reviewed_commits`集合（Campaignは単一のtyped `artifact_reviewed_commit`）と、実行時に指定repositoryで観測した`observed_execution_repository_head`は別の証拠である。後者はcheckout HEADだけを示し、agentlab binary／Python moduleのprovenanceは示さない。
- `ExpectedFileArtifact`はsingle-link regular file専用、`ExpectedTree`はdirectoryと完全列挙した許可file集合専用であり、`bundle_root`は後者だけに属する。
- `ExpectedTree.file_artifacts`は全件をrequiredとして列挙し、expected file countとtree digestの一致を必須にする。tree内のoptional fileでdigest検証を省略しない。
- `request_correlation_id`は同じRequestを相関させるIDであり、publication内容のIDではない。内容の識別にはmetadata内のInventory／Markdown SHA-256を使う。
- accepted／complete profileはArtifact内部commitの照合を必須にする。abandoned／missing profileで内部commitが存在しない場合は`not_verifiable`であり、commit mismatchではない。
- root／親symlink、path escape、読取race、identity未確定はexit 1で新しいcomplete publicationを作らない。安全に観測できたfinal symlink／hardlink／special fileは`unsafe_artifact` finding、exit 2とする。
- 3出力のpublicationはprocess内best-effort rollbackとincomplete publication検出を採用する。クラッシュatomic性は保証しない。
- retentionの`external_copy_receipt_verified`はreceiptの検証だけを意味し、`verification_basis=receipt_only`、`remote_liveness=not_checked`を必ず表示する。
- read-only snapshotの上限はRequest 4 MiB、1 Artifact file 64 MiB、tree 4,096 files／256 directories／4,096 entries／256 MiBとし、超過は安全なsnapshot不能としてexit 1にする。
- Summaryは`primary_campaign_count`、`provider_call_count_observed`、`provider_call_count_unknown_runs`、`campaigns_without_total`を保持し、drifted／not-verifiable Campaignもtotal未確定として可視化する。

## Non-goals

Provider、Prompt、Gate、Campaign、Report、Public Suiteの実行・再生成、network接続、Artifactの移動・削除・修復、Phase statusの自動更新は行わない。通常テストはsynthetic Artifactだけを使う。

## Finding and exit policy

FindingCodeは実装moduleの閉じたEnumからのみ出力し、detailは固定templateから生成する。OS例外、absolute path、Artifact本文、raw command outputは保存しない。観測可能なmissing／drift／binding不整合はexit 2のfailure Inventoryにし、構築不能・unsafe snapshot・publish不能はexit 1とする。

## Ownership

Phase 7A maintainerがschemaと安全境界を保守し、Phase 6 ownerが既存Artifactの意味とreviewed commitの正本を保守する。real `.artifacts/`へのInventory実行は、個別の人間承認後に限る。

## Slice 7A-4R2 — pre-publication contract remediation

Slice 7A-4R2は、実Artifact Requestを作成又は実行する前のcontract remediationである。Schema `1.0`はまだ公開Inventoryを持たないため、今回の修正はSchema `1.1`へのversion bumpではなく、未公開`1.0`のfail-closedな事前公開修正として扱う。旧field shapeの互換loaderは提供しない。

- Releaseは単一の`artifact_reviewed_commit`ではなく、昇順・重複なしの`artifact_reviewed_commits`でcommit集合を宣言する。`internal_required`では空集合を拒否し、観測集合とのexact matchだけをverifiedとする。
- 観測commitはgeneric JSON／JSONL／YAMLのkey探索から取得しない。validated Public Suite snapshotではPrimaryのtyped Spec 2.1／Plan 1.2（Fixture Acceptance bindingを含む）共有commitだけ、Historicalでは`HistoricalVerificationRecord.source_reviewed_commit`だけを採用する。bundle run、report、Markdown、mirror、未知role、`verification_agentlab_commit`はcommit Authorityではない。
- Campaignの`campaign_id`はtracked Authority上のCampaign ID、`experiment_id`はstrict-loadしたCampaign started eventのExperiment IDである。primary／historical profileは両方を必須とし、denominator、subject digest、Inventory、Markdownで別fieldとして保持する。run ID単独のRequest全体unique制約は置かない。
- Manifest referenceは`dirname(suite_manifest.path)`、rendered bundle memberは`bundle_root.root_path`を基準にcanonical repository-relative exact pathへ解決する。suffix検索は行わない。
- input aliasは同一Releaseのscalar `checksums`と同じRelease `bundle_root/checksums.json` memberだけに限定する。resolved path、bytes、SHA-256、`required=true`がすべて一致しない限り拒否する。

### Subject digest v2

tree digestのdomainは変更しない。subject digestだけは次のexact byte sequenceをSHA-256する。

```text
"agentlab.phase7.subject.v2\0" || subject_kind UTF-8 || "\0" || subject_id UTF-8 || "\0" || experiment_id UTF-8（Releaseは空） || "\0" || canonical JSON payload UTF-8
```

payloadは鍵順canonical JSONの`artifact_reviewed_commits`（昇順unique string list）、`files`、`trees`である。`files`はrequired scalarの`kind`、`role`、`path`、observed `byte_count`、observed `sha256`を`role,path`順で、`trees`はrequired treeの`kind`、`role`、`path`、observed `file_count`、observed `tree_sha256`を同順で列挙する。これ以外のArtifact本文、inode、absolute path、OS errorはpayloadへ入れない。

### Publication verification and Request preparation

`verify_evidence_inventory_publication_bytes(request_bytes, inventory_bytes, markdown_bytes, metadata_bytes)`は、exit `0`だけでなくfindingを含むexit `2` publicationもstrict/canonical reloadするpublic byte facadeである。Request SHA、correlation ID、inventory ID／scope／Authority references、metadata hash、expected HEAD、renderer/tool version、Markdown re-renderを照合する。

`publish-phase6-evidence-inventory-request`は実行commandではなく、stdinのcanonical Request bytesを固定root `.artifacts/phase7/evidence-inventory/<inventory_id>/request.json`へprepareするcreate-only commandである。human-approved `--expected-request-sha256`をconstant-time比較し、SHA mismatch・empty・noncanonical inputではfilesystemを一切変更しない。`.artifacts`は既存real directory必須、`phase7`と`evidence-inventory`はstable real directoryなら再利用、なければcomponentごとに安全作成する。inventory leafと`request.json`は再利用しない。leaf作成後の失敗では、このprocessが作成しidentityが一致するempty leaf／intermediateだけをrollbackし、変更・非empty・identity不一致なら保持して失敗とする。

`verify_declared_inventory_inputs()`とpublication本体は同じdescriptor-backed snapshot verifierを共有する。このcallはRequestのexpected bytes／hash／treeとそのcall内のsnapshot安定性を検証するだけで、別call間のinode不変を主張しない。

### Provider accounting and deferred pilot

Provider accounting scopeは常に`declared_campaign_entries`である。各Campaignは`observed`（integer / unknown `0`）、`partially_unknown`（known integer / unknown `>=1`）、`unavailable`（`null` / `null`）を持つ。missing、drifted、strict-load failure、total不在は`unavailable`として`campaigns_without_total`へ入り、0に変換しない。Campaign canonical finished eventだけを一度集計し、Evidence、Report、bundle mirrorは一致確認だけに使う。

Phase 6固有のcall対応と9/10 Authority値との関係は、read-only declarationである[Provider accounting crosswalk](PHASE7A_PHASE6_PROVIDER_ACCOUNTING_CROSSWALK.md)に固定する。crosswalkはPhase 6 status又は9/10 Authority accountingを変更しない。

Slice 7A-5のcurrent-only pilotは未実行であり、完了扱いではない。accepted superseded mirrorはownership-aware schemaを別途設計するまで未収載、Authorityがないcandidate／空directoryも未収載である。7A-4R2ではProvider、Gate、Replay、network、実Campaign、実Inventory、new model fixingをすべて0件とする。
