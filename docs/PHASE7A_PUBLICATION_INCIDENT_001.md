# Phase 7A Publication Incident 001

## Incident identity and authority boundary

- Incident ID: `phase6-accepted-current-pilot-001`
- Classification: incomplete publication / non-authoritative Phase 7 publication incident record
- Scope: current-only Pilot 001
- Phase 7A status: Slice 7A-5R1 implementation review approved; Slice 7A-5 Pilot incomplete

この記録は、Phase 7のpublication経路で発生した事象を保存する非権威的な記録である。Phase 6の9/10 Provider Authority、accepted release、Phase 6 status、current、supersession、denominator又は既存Artifactの意味を変更しない。Phase 6 Artifactはread-onlyで扱われた。

## Publication result

Pilot 001はexit `1`で終了した。Requestのread-only input verificationとInventory内容の検証は成功したが、3出力のcomplete publicationは成立していない。

001 root:

`.artifacts/phase7/evidence-inventory/phase6-accepted-current-pilot-001/`

再確認時のroot内容は次の2つだけである。

| Path | State | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| `request.json` | approved input; regular file; single link | 19,751 | `e3612ccd65742ac949b73d649f7475839c19f41e24b7a508de6262073428af22` |
| `evidence-inventory.json` | partial output; regular file; single link | 28,712 | `461abc05ac3a83a4e0e46b7e21999bf5271917d3dc111c1a759bdd88a338384f` |

`evidence-inventory.md`と`evidence-inventory.metadata.json`は不存在であり、hidden staging fileも不存在である。したがって、001はcomplete publication triadではなく、accepted Inventoryでもない。

001は次の理由で再利用しない。

- 001 root、Request、partial JSONを削除、修復、rename、補完、上書き、再実行しない。
- partial JSON単体を正式なInventoryとして扱わない。
- 次回は`phase6-accepted-current-pilot-002`という新しいInventory IDと、新しいRequestを使用する。

## Triage record

repository外で保存されたpublication failure triage diagnostic recordの値は次のとおりである。

- Bytes: `13,835`
- SHA-256: `0609e0b885d6f04166c628ecca61f3dc4e603a6ad9acab374e8d356744884d32`
- Original CLI exit: `1`

## Post-input verification

partial publication後の同一Requestに対するread-only post-input verificationは次の結果だった。

- `verification_status=verified`
- findings: `0`
- primary campaigns: `2`
- Provider: `10 observed / 0 unknown runs`
- `campaigns_without_total=0`
- Release／Campaign: 全件 `verified`
- Retention: 全4件 `local_only`
- Phase 6 input Artifactの変更: `0`

この成功は、Inventory内容と入力snapshotの検証結果を示すだけであり、Markdown／metadataを含むpublication成立やInventoryのaccepted statusを示さない。

## Root cause

1. 入力snapshotの安全検証に使う完全directory identityを、owned publication parentにも継続利用した。
2. 1件目のInventory JSON公開によるpublisher自身の親directory metadata変更を、外部競合によるidentity changeとして誤検出した。
3. Markdown公開前の失敗後、rollbackも同じ古い完全directory identityを要求したため失敗した。
4. rollback failureが元のpublication failureを覆い、原因の分離を困難にした。

これは外部Provider、Gate、Replay、Campaign、Report、Public Suite、network又はPhase 6 Artifactの障害ではない。

## Slice 7A-5R1 remediation

修復結果は次のとおりである。

- publication専用の固定親FDを使用する。
- publication parentはdevice、inode、file type、modeでidentityを束縛する。publisher自身によって変化するparentのsize、mtime、ctimeは置換判定に使わない。
- publication parent配下のdescendantは、固定parent FDを起点に従来の完全snapshot identityで再検証する。
- output fileはdevice、inode、full mode、nlink、size、mtime_ns、ctime_nsを含む完全identityで追跡する。
- final fileとstaging fileのowned cleanupを全対象について試行し、失敗を個別に収集する。
- unlink後の同名path残存を検出してrollback failureへ含める。
- descriptor closeは試行前に管理参照を切り離し、closeが実際にFDを解放してからerrorを返した場合でも同じFDを再closeしない。

Remediation commits:

- `9159cdded89a317a88a9a1e07f9ffa832b7ed7cc`
- `847dcb6a1831b1369cde9e5cee4b71cf492af569`
- `05228ede53dfdf9345e567e81de85c19c9bad40c`
- `c60546c275ac4963b88d5087a206fdbd06c215a6`

## Decision and next step

- Slice 7A-5R1 implementation review: approved
- Slice 7A-5 Pilot: incomplete
- Next Inventory ID: `phase6-accepted-current-pilot-002`
- 002 Requestは、文書化commit後に確定した新しい最終HEADへ束縛し、Request modelから再構築する。既存001 Requestを再利用しない。
- 002 Request Candidate Reconciliation、Request publisher、Inventory CLIはそれぞれ別の人間承認を必要とする。

この記録の作成自体はPhase 6 status、accepted relation、supersession、Provider Authority又は実Artifactを変更しない。
