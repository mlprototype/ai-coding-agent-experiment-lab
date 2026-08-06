# Phase 7A Evidence Inventory — Authority Decision

## Decision

Phase 7AのEvidence Inventoryは、Phase 6の既存Artifactと人間がレビューしたRequestを検証して表示する非権威的な派生成果物である。`authoritative=false`を変更せず、Phase 6のstatus、current、supersession、Artifact本文を更新しない。

## Fixed boundaries

- Phase 6 Artifactの`artifact_reviewed_commit`と、実行時に指定repositoryで観測した`observed_execution_repository_head`は別の証拠である。後者はcheckout HEADだけを示し、agentlab binary／Python moduleのprovenanceは示さない。
- `ExpectedFileArtifact`はsingle-link regular file専用、`ExpectedTree`はdirectoryと完全列挙した許可file集合専用であり、`bundle_root`は後者だけに属する。
- `request_correlation_id`は同じRequestを相関させるIDであり、publication内容のIDではない。内容の識別にはmetadata内のInventory／Markdown SHA-256を使う。
- accepted／complete profileはArtifact内部commitの照合を必須にする。abandoned／missing profileで内部commitが存在しない場合は`not_verifiable`であり、commit mismatchではない。
- root／親symlink、path escape、読取race、identity未確定はexit 1で新しいcomplete publicationを作らない。安全に観測できたfinal symlink／hardlink／special fileは`unsafe_artifact` finding、exit 2とする。
- 3出力のpublicationはprocess内best-effort rollbackとincomplete publication検出を採用する。クラッシュatomic性は保証しない。
- retentionの`external_copy_receipt_verified`はreceiptの検証だけを意味し、`verification_basis=receipt_only`、`remote_liveness=not_checked`を必ず表示する。
- read-only snapshotの上限はRequest 4 MiB、1 Artifact file 64 MiB、tree 4,096 files／256 directories／4,096 entries／256 MiBとし、超過は安全なsnapshot不能としてexit 1にする。

## Non-goals

Provider、Prompt、Gate、Campaign、Report、Public Suiteの実行・再生成、network接続、Artifactの移動・削除・修復、Phase statusの自動更新は行わない。通常テストはsynthetic Artifactだけを使う。

## Finding and exit policy

FindingCodeは実装moduleの閉じたEnumからのみ出力し、detailは固定templateから生成する。OS例外、absolute path、Artifact本文、raw command outputは保存しない。観測可能なmissing／drift／binding不整合はexit 2のfailure Inventoryにし、構築不能・unsafe snapshot・publish不能はexit 1とする。

## Ownership

Phase 7A maintainerがschemaと安全境界を保守し、Phase 6 ownerが既存Artifactの意味とreviewed commitの正本を保守する。real `.artifacts/`へのInventory実行は、個別の人間承認後に限る。
