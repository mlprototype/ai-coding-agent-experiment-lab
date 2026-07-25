# ADR 0003: コストとTokenを任意指標にする

- Status: Accepted
- Date: 2026-07-25

## Context

ProviderやCLIによってToken、cached Token、reasoning Token、quota、費用の公開範囲と
定義が異なる。CLI更新や契約プランでも取得可能性が変わる。これらを必須にすると、品質
結果を取得できているrunまで保存不能になり、欠損を0で埋めると分析を誤る。

## Decision

UsageMetricsの各値とsourceを任意にし、RunMetrics全体もUsageMetricsなしで保存できる。
値がある場合は`provider_reported`、`estimated`、`not_available`のsourceで由来を示す。
コスト計算はPhase 0で実装しない。欠損は0へ変換せず、比較時に欠損として扱う。

## Consequences

- Usage非対応ProviderやReplayでも品質・時間の結果を保持できる。
- Provider報告値と推定値を区別できる。
- コスト最適化の比較では欠測biasを明示する必要がある。
- 将来、通貨、価格表version、単位を含む別契約が必要になる可能性がある。

