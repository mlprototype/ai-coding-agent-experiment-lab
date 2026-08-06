# Phase 7A / Phase 6 Provider accounting crosswalk

This is a read-only, tracked declaration for Slice 7A-4R2. It binds the Phase 6 facts needed to review a future **current-only pilot** Request; it does not create an Inventory, alter `authoritative=false`, alter accepted/current/supersession, or change the Phase 6 Authority total of `9または10 calls`.

The generic Inventory renderer must not hard-code any number below. It renders only `provider_accounting_scope=declared_campaign_entries`, each Campaign's `observed` / `partially_unknown` / `unavailable` state, and the general non-project-wide warning.

## Read-only source declarations

| Source | Repository-relative path | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| Accepted Manifest | `.artifacts/phase6/public-suite-inputs/phase6-java-evaluated-0e6d894d-001/suite-manifest.json` | 7,548 | `88db41ae59fe03cff87d6d775cdded5dfdf6117bf73db02d36baad535a54b819` |
| Phase 6 Public Report | `docs/PHASE6_MULTI_LANGUAGE_PUBLIC_REPORT.md` | 31,886 | `400464fb8be03e4f313f697e01ba32dedb2d007426af92a499af9dbd2b26c00d` |
| Phase 6 ROADMAP | `docs/ROADMAP.md` | 16,505 | `fb9a9b38324e793366222ee7a648f091768cee96832ca0079290dd2fea2da340` |
| Java independent-004 supplemental approval | `.artifacts/phase6/live-approval-packets/0e6d894d797243f2a2e778afc09b221f51268210/java-independent-004/supplemental-approval.json` | 6,947 | `54da8177549e4a8351502b5ec523302a8d0baa9280681f18f9037a13f770c773` |
| Python independent-001 supplemental approval | `.artifacts/phase6/live-approval-packets/8a0aa7042ad1861d8d1c89f44597fc7d1bdb191f/python-independent-001/supplemental-approval.json` | 6,410 | `6dd28613fdb95620e53c58fc2ba6a070e8208dce0652f05d53755c0bbcb45438` |
| Historical verification record | `.artifacts/phase6/public-suite-inputs/phase6-java-evaluated-0e6d894d-001/historical/000/historical-verification-record.json` | 1,533 | `e3245caa71d6e9ca4697af325c4efdef675c3a404dc0713217479392a4c4ee49` |

The source paths above are declarations for a later `provider_accounting_declaration` AuthorityReference. They contain no absolute path, inode/device, prompt, raw reasoning, token, session, or secret value.

## Declared Campaign mapping

| Campaign Authority ID | Experiment ID | Canonical Campaign path / bytes / SHA-256 | Terminal timestamp | Provider count | Run-ID collision / mirror treatment | Phase 6 Authority 9/10 relation |
| --- | --- | --- | --- | ---: | --- | --- |
| `java-independent-004` | `phase6-java-workflow` | `.artifacts/phase6/public-suite-inputs/phase6-java-evaluated-0e6d894d-001/languages/java/campaign-artifacts/campaign.jsonl` / 2,767 / `b8b0f08303ee841a1b2562926f11b11b7b9e9a845b4d175ce2c7b0cbec702aee` | `2026-08-03T14:54:40.945981Z` | 2, unknown 0 | Its two run IDs were reused by earlier Java audit Campaigns, so run ID is not globally unique. This canonical finished event is counted once; public bundle and preparation mirrors are witnesses only. | included: accepted current primary contribution 2 |
| `phase6-python-workflow-independent-001` | `phase6-python-workflow-independent-001` | `.artifacts/phase6/public-suite-inputs/phase6-java-evaluated-0e6d894d-001/languages/python/campaign-artifacts/campaign.jsonl` / 2,875 / `3ed10f628e834c3a17a13e5d82717ed24f6da1c5fe455948eecfdd3d71e3d73b` | `2026-08-02T10:03:36.281447Z` | 2, unknown 0 | No collision with the other two declared Campaigns. Canonical Campaign input is counted once; bundle/preparation copies are not accounting sources. | included: accepted current primary contribution 2 |
| `workflow-ab-codex-live-002` | `workflow-ab-codex-live-002` | `.artifacts/phase6/public-suite-inputs/phase6-java-evaluated-0e6d894d-001/historical/000/campaign.jsonl` / 5,578 / `61312a960d152e90ca6dacd191ef9b541719a1aab634a7a1864eab2f977f1515` | `2026-07-28T11:05:11.761906Z` | 6, unknown 0 | Six historical run IDs are distinct from the two current-primary Campaigns. It is a Manifest historical source; its report/bundle mirror is validation evidence, not a second count. | excluded: Phase 4 historical verification, not part of Phase 6 9/10 Authority total |

### Run and evidence/recording binding

`java-independent-004` has the following two terminal runs, each with provider count `1`:

- `phase6-java-workflow_tag-normalizer_r001_staged_8d5a72fbe2a4` — Evidence `.artifacts/phase6/public-suite-inputs/phase6-java-evaluated-0e6d894d-001/languages/java/campaign-artifacts/evidence/phase6-java-workflow_tag-normalizer_r001_staged_8d5a72fbe2a4.json` / 10,430 / `db4e0e0c856e11e73413553048bd0e8acbe84a706a97ddc45712b98075199a5b`; Recording `.artifacts/phase6/public-suite-inputs/phase6-java-evaluated-0e6d894d-001/languages/java/campaign-artifacts/recordings/phase6-java-workflow_tag-normalizer_r001_staged_8d5a72fbe2a4.jsonl` / 8,255 / `d57d8fbd070f43c76cc0c61f07bcc594e290a17a41167cd8448bb7ddf77ee920`.
- `phase6-java-workflow_tag-normalizer_r001_one_shot_6011a936ff4a` — Evidence `.artifacts/phase6/public-suite-inputs/phase6-java-evaluated-0e6d894d-001/languages/java/campaign-artifacts/evidence/phase6-java-workflow_tag-normalizer_r001_one_shot_6011a936ff4a.json` / 10,434 / `99315b5472564f440e36b582f821e02aa27849b24fce7a64e76e97e9aa885d8f`; Recording `.artifacts/phase6/public-suite-inputs/phase6-java-evaluated-0e6d894d-001/languages/java/campaign-artifacts/recordings/phase6-java-workflow_tag-normalizer_r001_one_shot_6011a936ff4a.jsonl` / 8,269 / `2e96ca2c8efc7ce93a331d4772daa1e7820503dbb885ef43c07ca4abf83ad4e8`.

`phase6-python-workflow-independent-001` has the following two terminal runs, each with provider count `1`:

- `phase6-python-workflow-independent-001_tag-normalizer_r001_one_shot_9245c75efd86` — Evidence `.artifacts/phase6/public-suite-inputs/phase6-java-evaluated-0e6d894d-001/languages/python/campaign-artifacts/evidence/phase6-python-workflow-independent-001_tag-normalizer_r001_one_shot_9245c75efd86.json` / 8,977 / `a3e27aabb6609a66f95cdd3f634ffc4d6ab56a79ded2f7b767feb2567819c715`; Recording `.artifacts/phase6/public-suite-inputs/phase6-java-evaluated-0e6d894d-001/languages/python/campaign-artifacts/recordings/phase6-python-workflow-independent-001_tag-normalizer_r001_one_shot_9245c75efd86.jsonl` / 7,048 / `ddc24e80c53472fb61b53c7e1d836734fcdbef824e72365c1fb1158979934f25`.
- `phase6-python-workflow-independent-001_tag-normalizer_r001_staged_e0c58ef53e99` — Evidence `.artifacts/phase6/public-suite-inputs/phase6-java-evaluated-0e6d894d-001/languages/python/campaign-artifacts/evidence/phase6-python-workflow-independent-001_tag-normalizer_r001_staged_e0c58ef53e99.json` / 8,977 / `b33bf53d42e551ac0e9ff3946401fc26ef041071fbe602ae22c6d3bda5b5a319`; Recording `.artifacts/phase6/public-suite-inputs/phase6-java-evaluated-0e6d894d-001/languages/python/campaign-artifacts/recordings/phase6-python-workflow-independent-001_tag-normalizer_r001_staged_e0c58ef53e99.jsonl` / 7,046 / `866b72ef078292168890ceef2b2d9c8787211522cf2da68f224504ff908f1526`.

`workflow-ab-codex-live-002` has six terminal runs, each provider count `1`: `workflow-ab-codex-live-002_tag-normalizer_r001_staged_ed3d89b46bcc`, `workflow-ab-codex-live-002_tag-normalizer_r001_one_shot_d214a4361d98`, `workflow-ab-codex-live-002_tag-normalizer_r002_staged_5fc32b10f37f`, `workflow-ab-codex-live-002_tag-normalizer_r002_one_shot_270a07922f71`, `workflow-ab-codex-live-002_tag-normalizer_r003_one_shot_f4fd98c74f8d`, and `workflow-ab-codex-live-002_tag-normalizer_r003_staged_1b2942750181`. Its historical source declares no Recording/Evidence pair for this accounting; the verification record, plan, Campaign, report JSON, and report Markdown are the Manifest-listed binding set.

## Reconciliation boundary

For a later Request declaring exactly the three rows above, the Request-scoped sum is primary `4` plus historical `6`, or `10 observed / 0 unknown`. This is not a restatement of project-wide consumption: the Public Report's Phase 6 Authority range `9または10` includes Phase 6 primary/audit/health-check accounting and excludes the Phase 4 historical six-call Campaign. The one uncertain old Python abandoned Campaign is the source of the Authority range; it is not a declared row here.

Accepted superseded mirrors and the candidate empty input directory remain unlisted. Their ownership and duplicate-accounting behavior need a separate mirror-aware schema decision before a whole-Phase-6 Inventory can be completed.
