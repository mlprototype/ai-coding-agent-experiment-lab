# ADR 0005: Codex Liveは正規化summaryだけをRecordする

- Status: Accepted
- Date: 2026-07-26

## Context

Codex CLIへ渡すPrompt、JSONL event、stderr、agent message、reasoning、command outputには、
実験に不要な機密情報、認証状態、session識別子、長大なpayloadが含まれうる。一方、
再現性には実行条件、lifecycle、Usage、終了状態、品質Gate、diffの証跡が必要である。
ProviderとGateが同じ親環境を継承すると、CLI認証情報が評価commandへ漏れる。

## Decision

Promptは通常fileから一度だけ読みstdinで渡し、本文を永続化対象へ入れない。SHA-256、
UTF-8 byte数、redacted flagだけを保存する。Codex JSONLは上限付きで逐次parseし、raw
payloadを破棄してevent件数、unknown件数、item type件数、terminal Usageだけを
CodexExecutionEvidenceへ正規化する。raw stdout/stderr、thread/session IDは保存しない。

Phase 3の認証は既存Codex CLIのChatGPT-managed authだけを対象とする。API keyを継承せず、
auth fileをcopy/parseしない。Codex processにだけ既存`CODEX_HOME` pathを渡し、Gateは
Phase 2 allowlist環境と別の一時HOME/TMP/cacheで起動する。

実Codex smokeは通常テスト/CIから分離し、レビュー後の明示確認で1回だけ行う。通常テスト
はfake executableだけを使う。

## Consequences

- Promptやraw eventから過剰な機密情報をArtifactへ持ち込まない。
- Recording 1.1をoffline Replayできるが、agentの会話やreasoning自体は再現しない。
- Provider schema追加は未知event件数として許容し、core lifecycle破壊だけをfail closed
  できる。
- ChatGPT-managed authがない環境や必須CLI flag不足ではLive実行できない。
- Codex model API通信は必要であり、OS-levelの完全なnetwork遮断は保証しない。
