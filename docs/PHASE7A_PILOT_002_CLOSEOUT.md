# Phase 7A Pilot 002 Current-Only Inventory Closeout

## Identity and decision

- Pilot ID: `phase6-accepted-current-pilot-002`
- Classification: successful current-only, non-authoritative Phase 7A Evidence Inventory pilot
- Schema version: `1.0`
- Execution HEAD: `2707c11bcd4ba3653fc976b08bf2a0e0cd48dc74`
- Generated at: `2026-08-08T14:30:05.333382Z`
- Inventory `authoritative`: `false`
- Scope: `phase6`

This closeout records one successful current-only pilot. It is not a Phase 6 status
record, accepted-release decision, supersession record, or project-wide Provider
accounting record. It does not complete Slice 7A-5 as a whole. Accepted superseded
mirror ownership remains a separate design decision.

The pilot scope was one accepted-current Release, two primary-evaluation Campaigns,
and one Manifest-required historical non-primary Campaign. Accepted superseded,
candidate, and Authority-less empty directories were excluded.

## Request and complete publication

All four files below were read back as single-link regular files and verified with
descriptor-backed identity checks. The 002 leaf contains exactly these four entries;
there is no hidden staging file.

| Path | Bytes | SHA-256 |
| --- | ---: | --- |
| `.artifacts/phase7/evidence-inventory/phase6-accepted-current-pilot-002/request.json` | 20,278 | `38a0aa3aa16a13fa7b7b572b67fe0060844af920770ba6f2ad2e51348357fbb0` |
| `.artifacts/phase7/evidence-inventory/phase6-accepted-current-pilot-002/evidence-inventory.json` | 29,348 | `c3db26f020b44b24a063a7299a7d615dfab281e938aedc9b7aded4f856e0cd6d` |
| `.artifacts/phase7/evidence-inventory/phase6-accepted-current-pilot-002/evidence-inventory.md` | 2,417 | `546f9f417b2db279c080bbf1790d15cbc5834921efbfa5fd10e5d49d8da622f2` |
| `.artifacts/phase7/evidence-inventory/phase6-accepted-current-pilot-002/evidence-inventory.metadata.json` | 677 | `5b4c9164135896fa387611497f7e2b213449264f11b44671802547b0f59137c4` |

The Request was published create-only from the human-approved candidate bytes.
The Request publisher was not rerun after publication, and the Inventory CLI was
run exactly once with exit `0`.

## Strict verification

The public byte facade
`verify_evidence_inventory_publication_bytes(request_bytes, inventory_bytes,
markdown_bytes, metadata_bytes)` successfully verified the complete publication.
The same Request was used for pre-input and post-input verification.

- Pre-input verification: `verified`, findings `0`
- Post-input verification: `verified`, findings `0`
- Inventory verification status: `verified`
- Findings: `0`
- Request correlation ID: `rc-8a7b50664f10fa91e3644d00d06111e7`
- Request SHA binding: PASS
- Inventory SHA binding: PASS
- Markdown SHA binding: PASS
- Markdown canonical rerender: byte-exact PASS
- Renderer version: `phase7-inventory-renderer-1.0`
- Tool version: `agentlab-phase7-1.0`
- Expected execution HEAD: `2707c11bcd4ba3653fc976b08bf2a0e0cd48dc74`
- Observed execution HEAD: `2707c11bcd4ba3653fc976b08bf2a0e0cd48dc74`
- Execution HEAD attestation: `6eecc8e0401097b636c1b68d432a5420d78f8d3c02fae754176058fbb8cdb91d`

## Inventory result

### Summary

- Release count: `1`
- Campaign count: `3`
- Primary campaign count: `2`
- Provider accounting scope: `declared_campaign_entries`
- Provider calls observed: `10`
- Provider unknown runs: `0`
- Campaigns without total: `0`
- Storage: present `4`, partial `0`, missing `0`
- Integrity: verified `4`, drifted `0`, not-verifiable `0`

The observed total of 10 is the sum for the Campaign entries declared in this
Request. It is not a replacement for, or a reinterpretation of, the Phase 6
project-level `9 or 10` Provider Authority. The tracked Provider accounting
crosswalk remains the non-authoritative declaration for that distinction.

### Provider accounting

| Campaign ID | Observed | Unknown runs |
| --- | ---: | ---: |
| `java-independent-004` | 2 | 0 |
| `phase6-python-workflow-independent-001` | 2 | 0 |
| `workflow-ab-codex-live-002` | 6 | 0 |

### Retention

All four subjects—one Release and three Campaigns—are:

- Retention state: `local_only`
- Verification basis: `local_artifact_only`
- Remote liveness: `not_checked`

No remote liveness check, External Copy, or External Anchor was created.

## Safety and authority boundaries

- Request publisher rerun: `0`
- Inventory retry/fallback: `0`
- Provider: `0`
- Gate: `0`
- Replay: `0`
- Campaign: `0`
- Report: `0`
- Public Suite: `0`
- External network: `0`
- Phase 6 Artifact changes: `0`
- Pilot 001 changes: `0`
- Source code, tests, schema, renderer, tool version, and dependencies changed: `0`
- Closeout commit: `1`
- Closeout push: `1`

The Inventory remains `authoritative=false`. Phase 6 status, accepted relation,
supersession, and the Phase 6 `9 or 10` Provider Authority were not changed.
This current-only result does not include accepted superseded mirror ownership and
does not make Slice 7A-5 complete. The next design task is a mirror-aware
Inventory contract for accepted superseded artifacts. Candidate and Authority-less
empty-directory handling also remains undecided.

## Pilot 001 preservation

Pilot 001 remains incomplete incident evidence and was not reused, repaired,
completed, deleted, renamed, or rerun. Its leaf still contains only the approved
Request and partial Inventory JSON:

| Path | Bytes | SHA-256 |
| --- | ---: | --- |
| `.artifacts/phase7/evidence-inventory/phase6-accepted-current-pilot-001/request.json` | 19,751 | `e3612ccd65742ac949b73d649f7475839c19f41e24b7a508de6262073428af22` |
| `.artifacts/phase7/evidence-inventory/phase6-accepted-current-pilot-001/evidence-inventory.json` | 28,712 | `461abc05ac3a83a4e0e46b7e21999bf5271917d3dc111c1a759bdd88a338384f` |

Pilot 001 Markdown, metadata, and hidden staging files remain absent. Any
pre-existing `.DS_Store` outside the 001 leaf is not a formal pilot entry and was
not changed.
