# Antigravity CLI Provider オフライン設計

## ステータス

- Phase: 5
- Phaseステータス: 実施中
- Slice 5Aステータス: 実装済み、オフライン検証済み
- Slice 5Bステータス: 設計のみ。実装は未承認
- 設計日: 2026-07-28
- Slice 5A是正およびSlice 5B設計更新日: 2026-07-29
- 設計ベース: `feature/phase5` の
  `d3a8cf814623ea0bdd071d12c948a582f38a827d`
- 実装担当: Antigravity

本書は、Antigravity CLI Providerの最初のオフラインSliceにおける実装契約と
完了境界を定める。また、Slice 5Bの設計Gateも記録する。ただし、Slice 5Bの実装、
実際のAntigravity agent実行、モデルへのリクエスト、認証フロー、quota使用、
Provider比較、Live Artifact作成は承認しない。

## 目的

完了済みのReplay、Safe Runner、Codex Provider、Workflow A/B経路の意味や互換性を
変更せず、Antigravity CLIを既存のProvider境界へ接続する。

最初のSliceでは、外部AI呼び出しなしで検証できる次の要素だけを確立する。

1. バージョン管理されたAntigravity契約
2. 厳格な`stream-json` Parser
3. 上限付き・read-onlyの`--version`および`--help` Preflight
4. 正規化・redaction済みEvidenceの構築
5. fake `agy`によるオフラインテスト
6. 未検証能力に対するfail-closed境界

## 信頼できる製品仕様

以下の仕様は、2026-07-28時点のGoogleドキュメントで確認した。これは製品
ドキュメントの内容であり、バージョン管理されたローカルPreflightの代替ではない。

- 実行ファイルは`agy`である。
- Headless modeは、`-p`、`--print`、または`--prompt`を使用する、1つのPromptと
  1回のprocess起動である。
- `--output-format stream-json`は、1つの`init`、0個以上の`step_update`、および
  1つのterminal `result`から成るNDJSONを出力する。
- `--model`はmodel slugを固定する。不明なmodelを指定した場合、別のmodelを
  暗黙に選択せず失敗する。
- `--effort`は`low`、`medium`、`high`のいずれかを受け付ける。
- `--print-timeout`はCLI自身の待機時間を制限する。一方、AgentLabは独立した
  process group timeoutを引き続き適用しなければならない。
- `--sandbox`はterminal sandbox制限を要求する。
- Headless modeはcache済みcredentialsを使用する。cache済み認証がない場合、
  非対話実行はbrowser入力を待たず、認証errorで終了する。
- 構造化出力は`stdout`へ、診断およびpermission通知は`stderr`へ出力される。
- Headless modeでは、利用できない承認を必要とするtoolがsoft-denyされても、
  実行を継続して終了コード`0`で終了する場合がある。
- Workspace内のfile read/writeは通常、自動的に許可される。shell commandや
  web accessを含むその他の操作は、実効permission policyに依存する。
- streamにはconversation ID、絶対path、response text、tool parameter、
  tool output、subagent metadata、usage dataが含まれ得る。raw stream内容は
  機密情報であり、永続化してはならない。

一次情報:

- [Headless mode](https://antigravity.google/docs/cli/headless)
- [Permission](https://antigravity.google/docs/cli/permissions)
- [Sandbox](https://antigravity.google/docs/cli/sandbox)
- [Installと認証](https://antigravity.google/docs/cli/install)
- [CLIリファレンス](https://antigravity.google/docs/cli/reference)
- [変更履歴](https://antigravity.google/changelog)

## 重大な未解決点と判断

### Live用Prompt transportは未解決

文書化されたAntigravityのHeadless interfaceは、`-p`/`--prompt`の値として
Promptを受け取る。文書化されたstdinまたは`--prompt-file` transportは
見つかっていない。そのため、生成したPromptを直接渡すとprocess argvへ露出する。

現在のrepository契約は、Provider Promptの内容をargvへ含めないことを要求する。
この要件を引き続き正とする。オフラインSliceで`AGENTS.md`を緩和したり、
Live用の例外を導入したり、文書化されていない`@file`形式をPrompt-file APIと
同等だと主張したりしてはならない。

したがって、次を境界とする。

- このSliceではproduction用`agy -p <generated-prompt>`を起動しない。
- このSliceでは`live-antigravity` CLI commandを追加しない。
- testまたはCIからAntigravityを呼び出さない。
- model呼び出しによるPrompt deliveryの証明を試みない。
- 将来のLive作業には、次のいずれかを選ぶ独立した人間の判断が必要である。
  - 文書化された非argv transportを待つ。
  - review済みで機密情報を含まない合成Promptに限ってargv露出を明示的に
    受け入れ、repositoryのsecurity契約を更新する。

### 終了コード0だけでは不十分

Headless permission requestはsoft-denyされる場合があるため、
`exit_code == 0`および`result.status == SUCCESS`は、要求したすべてのtoolが
実行されたことを証明しない。結果Workspaceの正しさは引き続きQuality Gateを
根拠とする。Evidenceはterminal statusからcommand、web、MCP、subagentの
実行成功を推定してはならない。

### Sandboxとpermissionに関する主張は別々に観測する

`--sandbox`はCLIへの要求である。すべてのProvider操作がsandbox内に収まった
証明ではなく、file、web、MCP、approval policyを単独で説明するものでもない。
要求した設定とstreamから観測した値を分けて永続化する。AgentLabのprocess group
runnerをAntigravity sandboxと表現してはならない。

### Provider内部のretryは不透明

AgentLabはretry、fallback、conversation resume、replacement runを行っては
ならない。Antigravityのserviceまたはharness内部で行われるretryはProviderの
挙動であり、AgentLabのretryではない。streamに安定した型付きfieldとして
公開されない限り、`0`ではなくunavailableとして記録する。

## スコープ

### 対象範囲: オフラインSlice 5A

- 新しいschema versionを持つAntigravity固有enumおよびEvidence model
- 未検証のproduction versionを選択しない、バージョン管理されたCLI profile model
- 渡されたbytesを対象とする厳格なNDJSON parsing
- terminal stateおよびProvider報告token usageの正規化
- 上限付きversion/help Preflight
- 意味が一致する場合に既存の共通primitiveを再利用するlifecycle/failure mapping
- fake executableおよびParser fixture
- 既存Artifactのbytesまたはloaderが変化しないことを証明する互換性test
- 後続のLive境界およびProvider比較境界の文書化

### 対象外

- 実際の`agy -p`実行
- 対話型`agy`、login、browser OAuth、keyring確認、logout、account変更
- `agy models`、`agy agents`、quota参照、model catalog参照、またはservice requestを
  必要とする可能性があるcommand
- Antigravity subscriptionまたはAPIの使用
- 実model、Prompt、Fixture、Workspace、Gateの実行
- `--dangerously-skip-permissions`
- global `~/.gemini` fileの変更
- 一時HOMEまたは認証の実験
- Provider比較Spec、scheduler、report、winner選択
- Phase 4 Campaignの変更またはreplay
- PR作成またはmainへのmerge

## アーキテクチャ

Antigravity契約はCodex固有契約に隣接させつつ、独立させる。

推奨module:

- `src/agentlab/antigravity_provider.py`
  - version/help Preflight
  - profile選択
  - 厳格なstream Parser
  - 正規化helper
  - Slice 5Aではproduction用Live process runnerを追加しない
- `src/agentlab/models.py`
  - Antigravity enumおよび後方互換なEvidence 1.0/1.1
  - 意味が一致する場合に限った共通`UsageMetrics` mapping
  - 既存Codex schema versionを変更しない
- `tests/test_antigravity_provider.py`
  - Parser、Preflight、Evidence、redaction、failure matrix
- `tests/fixtures/antigravity/`
  - inline test dataよりfixtureの方が明確な場合に限り、小さな合成NDJSON sampleを置く
  - 実conversationまたはuser dataをcopyしない

対称性だけを目的として抽象Provider hierarchyを導入してはならない。現在の契約が
真にProvider-neutralである場合、process group、strict JSON、time、redaction、
atomic persistence helperをcompositionで再利用する。Antigravityへ合わせるために
Codex fieldをrenameしたり、Codex validatorを緩和したりしてはならない。

## バージョン管理されたPreflight

### 許可する操作

オフラインPreflightで実行してよいのは次だけである。

```console
agy --version
agy --help
```

production実装では、argv array、`shell=False`、分離されたstdout/stderr、bytes上限、
厳格なtimeout、新しいprocess group、残存process cleanup、sanitized environmentを
使用しなければならない。通常testではfake executableを必須とする。

Preflightで`agy -p`、`agy models`、認証command、model catalog呼び出し、
sample taskを実行してはならない。

### 必須help marker

最初に選択可能とするprofileは、文書化された次の能力をすべて必須とする。

- `--prompt`または文書化された同等のHeadless alias
- `--output-format`
- `stream-json`
- `--model`
- `--effort`
- `--print-timeout`
- `--sandbox`

`--dangerously-skip-permissions`の存在は必須とせず、AgentLabが使用する能力としても
扱わない。

### Version方針

web pageまたはcodelabのversion番号をproduction allowlistへcopyしてはならない。
userのローカル`agy --version`結果を後でreviewし、別commitで厳密なprofileとして
登録しなければならない。

それまでは次のように扱う。

- commandを利用できない: `not_verified`
- version/help失敗: Preflight失敗
- 対応flagは存在するがversion未登録: profile `not_selected`
- profile未選択: Provider起動禁止

## 厳格な`stream-json`契約

### フレーミング

- inputはUTF-8 NDJSONとする。
- 空でない各lineは、ちょうど1つのJSON objectでなければならない。
- 不正なUTF-8、重複key、非有限数、object以外の値、空line、上限を超えるline、
  設定した上限を超える総outputをrejectする。
- 任意のbyte chunk境界をまたいでincrementalにparseする。
- raw lineは、上限付き正規化の完了後に破棄する。

### Eventの順序

最初のバージョン管理profileでは、次の順序を必須とする。

1. 最初に`init` eventがちょうど1つ
2. `step_update` eventが0個以上
3. 最後に`result` eventがちょうど1つ
4. `result`の後にeventがない

不明なtop-level eventは`provider_protocol_error`としてfail-closedにする。
不明な`step_type`値も、明示的にreviewして追加するまでは、選択済みversion profileで
fail-closedにする。schema driftを暗黙にsuccessへmappingしてはならない。

### 正規化field

上限付きで内容を含まない次の観測値だけを永続化する。

- 固定enumごとのevent count
- 固定`step_type`ごとのstep count
- `init.permission_mode`
- 要求したmodel fieldおよびagent fieldが存在したか
- terminal status
- terminal `num_turns`
- 有限性と範囲を検証した後のterminal duration（milliseconds）
- Provider報告usage
- stdout/stderrのbyte countおよびtruncation flag
- process lifecycleおよびcleanup結果

次は永続化しない。

- `conversation_id`またはsession identifier
- `cwd`またはローカル絶対path
- tool list
- responseまたは`text_delta`
- error message text
- tool名、parameter、output、error text
- subagent role、conversation ID、log URI、Workspace URI
- raw stdout/stderrまたはraw NDJSON
- reasoningまたはagent message

### Terminalの対応付け

- `SUCCESS`
  - 1つのterminal result、process exit `0`、`num_turns == 1`を必須とする。
  - Providerがresponseを生成したことを意味するが、すべてのtool実行を意味しない。
- `ERROR`
  - Provider失敗。保守的でredaction-safeなclassifierを利用できる場合に限り、
    上限付きの参考hintを分類する。
- `CANCELED`または`INTERRUPTED`
  - Provider interruption。Harness cleanup失敗とは区別する。
- `INVALID`、`WAITING`、または`RUNNING`
  - 完了済みone-shot runとして不正なterminal state。
- resultの欠落・複数存在、resultが最後でない、不正field、schema drift
  - protocol失敗。
- timeout、signal termination、spawn失敗、output limit、collection失敗、
  process cleanup失敗
  - Codex境界と同様に、別々のHarness/Provider failure kindとして維持する。

### Usage mapping

Providerが報告したinteger値だけをmappingする。

| Antigravity result | AgentLab |
|---|---|
| `input_tokens` | `input_tokens` |
| `cache_read_tokens` | `cached_input_tokens` |
| `output_tokens` | `output_tokens` |
| `thinking_tokens` | `reasoning_output_tokens` |

`total_tokens`はcross-check fieldであり、AgentLabの追加metricではない。文書化された
exampleではthinking tokenをoutputの一部、cache-read tokenをinputの一部として
扱うため、5つのfieldをすべて加算してはならない。

boolean、string、負数、overflow、不整合なtotalをrejectする。terminal usage objectが
ない場合、null値とともに`not_available`を永続化する。欠落usageを0へ変換しては
ならない。

## Antigravity Evidence 1.0および1.1

`extra="forbid"`を設定した専用のstrict modelを定義する。最低限、次を記録する。

- schema versionおよび`provider=antigravity`
- 厳密なCLI versionおよび選択済みprofile
- Preflight timestampおよびverified flag
- 要求したmodel slugおよびeffort
- 要求したoutput format
- Prompt transport state
- Promptのargv露出が発生するか
- execution、invocation、cleanup stage
- 要求したsandbox flag
- 利用できる場合は観測したpermission mode
- raw stream persistence `false`
- Provider statusおよび正規化済みterminal status
- event/step count
- usage sourceおよび正規化済み値
- stdout/stderrのbyte countおよびtruncation
- timeout/signal/process group termination結果
- 固定failure kindおよびfailure stage

Evidence 1.0は引き続きload可能とし、後から導入したfieldを禁止する。
Evidence 1.1は、構造化Preflight結果が渡された場合に限って出力する。
`version`と、試行した場合は`help`を含む、順序付きでredaction済みの
`preflight_commands` listを追加する。各entryにはreturn code、stream別byte countと
truncation flag、failure stage/kind、process group termination evidenceを記録する。
rawのversion/help outputは記録しない。

Evidenceは、要求値、観測値、利用不能値を区別しなければならない。安定した観測に
よってその事実を証明できない限り、sandbox、認証、Prompt delivery、model API受信、
quota消費、tool実行、network blockingが成功したと記載してはならない。

既存のRecording 1.0/1.1、Codex Evidence 1.1-1.5、Live Artifact 1.0/1.1、
Failure Diagnostic 1.0、Workflow Plan、Campaign、report loaderは、bytesおよび
schemaの互換性を維持しなければならない。

### Slice 5A是正の完了条件

2026-07-29の是正では、Antigravity Evidence 1.0のload契約を維持し、構造化された
Preflight観測用にEvidence 1.1を追加する。

- 最終newlineのない末尾NDJSON lineは正規化直後にParser bufferからclearし、
  failure pathでは未処理のbuffered bytesをすべてclearする。
- `provider_output_limit`は、上限付きPreflight stderrを含むstdoutまたはstderrの
  truncationを表現できる。ただし、少なくとも1つのstreamがtruncatedであり、
  同じstreamにbytesが存在することを必須とする。
- process groupのliveness確認中に発生した`PermissionError`は生存として扱う。
  不明な`OSError`はcleanup観測失敗のまま維持し、いずれも後続確認でcleanup成功へ
  戻してはならない。
- cleanup失敗はtimeout、output limit、collection失敗より高いpriorityを維持する。
  同時に観測した内容は`preflight_commands`内にnestedしたまま保持する。
- Evidence 1.1のstrict loaderは、選択済みprofile、Provider invocation、Provider
  successに正常な`version` → `help`の両commandを必須とする。nested commandが
  失敗した場合はこれらの状態を禁止し、cleanupを最優先とするfailure kind/stageを
  top-levelと一致させる。

production用Antigravity CLI version allowlistは空のままとする。選択可能profileおよび
streamの全acceptance testでは、注入したversion allowlistと合成local executableだけを
使用する。

## オフライン受入テスト

Antigravity実装では、少なくとも次のtestを追加しなければならない。

### Preflight

- `agy`が存在しない場合
- stdoutまたはstderrからversion/helpを正常取得する場合
- 非0 exit
- timeout
- spawn/collection/process cleanup失敗
- 各必須flagがそれぞれ欠落する場合
- 厳密なversionが未登録の場合
- `--version`と`--help`だけを起動したことの証明
- fake processが親processのsecret environmentを継承しないこと

### Parser

- 正常な`init` → `step_update`* → `result`
- 任意のchunk境界およびmultibyte UTF-8の分割
- step updateが0個の場合
- textを保持しない複数の`agent_response` delta
- toolおよびsubagent payloadの破棄
- 重複key、不正UTF-8、空line、object以外のJSON
- 上限を超えるlineおよびtotal output limit
- resultの欠落、重複、早すぎる出現、最後以外への出現
- result後のevent
- 不明eventおよび不明step type
- すべてのterminal status
- 非0 exitまたは`num_turns != 1`の`SUCCESS`
- usageの存在、欠落、不正形式、負数、boolean、非有限数、overflow、不整合なtotal

### Evidenceと互換性

- serialize済みEvidenceにPrompt、response、error text、conversation ID、path、
  tool payload、raw stream、auth値、secretが含まれないこと
- strict round-tripおよび不明fieldのreject
- 欠落usageが欠落のままであること
- 固定failure kindが区別されたままであること
- 外部AI、network、auth、model catalog、Gate、実際の`agy -p`を使用しないこと
- 既存のReplay/Codex/Workflow testを変更せず、すべてpassすること

2026-07-29のSlice 5A acceptance suiteは、合成bytesまたは短時間で終了するlocal fake
executableを使用して、列挙した契約を検証する。明示的な境界caseには、実値
65,536/65,537-byteのParserおよびPreflight stream、`os.set_blocking`、`select`、
pipe readのcollection失敗、direct childおよびgrandchildの消滅、
`PermissionError`および不明`OSError`のcleanup観測、terminal numeric bounds、
すべてのterminal-status-to-failure-kind mapping、不正usage class、末尾lineの
raw buffer除去、Preflight stderrからEvidence 1.1への伝播を含む。

これは範囲を限定した受入表明であり、考え得るすべてのOS、pipe、scheduler、
将来のCLI挙動を網羅したという主張ではない。test countは契約の一部として固定せず、
`pytest --collect-only`およびfull suiteの実行結果から報告する。

必須検証:

```console
uv run pytest
uv run ruff check .
uv run mypy src
uv run agentlab doctor --json
```

`doctor --json`は、既存のread-only version/help probeを使用する場合がある。testでは
引き続きfakeを使用しなければならない。developer machineでdoctorを実行すると実際の
`agy`へ触れる場合は、別途報告し、オフラインtest結果として扱わない。

## 後続Slice

### Slice 5B: Headless Runner準備とオフライン統合

**ステータス: 設計のみ。実装は未承認。**

Slice 5BはSafe Subprocess runner境界を準備し、短時間で終了する合成local
executableだけで証明する。実際の`agy`を起動する、Promptを送信する、認証する、
model catalogまたはquotaへaccessする、外部networkを使用する、Live Artifactを
作成する、のいずれも行ってはならない。

#### BlockerとなるPrompt transport判断

文書化されたCLI契約では、Prompt値を引き続きargvへ置く。AgentLabのrepository契約は
Prompt内容のargv格納を引き続き禁止し、stdinだけでのPrompt deliveryを許可する。
stdinを受け付けるfake executableはAgentLab collectorを証明できるが、実際の
Antigravity CLIがそのtransportをsupportする証明にはならない。

したがって、別途reviewした判断で次のいずれかを行うまで、Slice 5B実装は
fail-closedのままとする。

1. 文書化され、ローカルPreflight済みの非argv Prompt transportを登録する。
2. 上限付きで機密情報を含まない合成Promptについてrepositoryのsecurity契約を
   明示的に変更し、受け入れたargv露出を記録する。

文書化されていない`@file`、stdin、environment variable、shell expansion、
temporary fileの規約を、production用Antigravity Prompt transportとして扱っては
ならない。

#### 開始条件

- Slice 5A是正がreview済みで、すべてのオフライン検証がpassしていること
- 上記のPrompt transport判断が、別のreview済み設計変更として記録されていること
- 厳密なローカル`agy X.Y.Z`と必須help markerがreviewされ、1つのimmutable profileに
  登録されていること
- 一時HOME、cache済み認証の挙動、immutableなrun-local settings、sandbox request、
  web/MCP/approval policyについて、観測以上に強い隔離を主張せず仕様化していること
- 厳密なmodel slug、reasoning effort、CLI timeout、Harness timeout、byte limit、
  stop conditionが固定されていること
- create-onlyのRecording/Evidence/Diagnostic pathおよびcleanup責任を定義していること

#### 別途承認された場合のRunner契約

- profile選択を必須の前提条件とし、`NOT_SELECTED`ではspawnを禁止する。
- 1 runは1つの新規conversation、1 process、1 Prompt、最大1回のProvider呼び出しとする。
- argvは`shell=False`を指定した明示的なarrayとし、stdoutとstderrを分離したままにする。
- 解決済みPrompt adapterを唯一のPrompt transportとし、Prompt内容をlogまたは
  永続化しない。
- ProviderとGateのenvironmentを分離し、親processのsecret variableを継承しない。
- stdoutは`StrictAntigravityStreamParser`へincrementalに渡す。stderrはcountし、
  上限付きで分類した後に破棄する。
- timeout、signal、output limit、collection、spawn、cleanupの失敗は互いに排他的な
  failure kindとして維持し、quality-gate failureへ変換しない。
- raw JSONL、stderr、agent text、reasoning、tool input/output、subagent payloadを
  永続化しない。
- retry、fallback、continuation、resume、replacement run、subagent delegationを
  行わない。

#### 別途承認された場合のオフライン受入条件

- 合成local executableだけを使用する。
- 非argv transportでは、argv、log、Evidence、Recording、Diagnostic、process metadataに
  Promptが存在しないことをassertする。
- 厳密なargv、sanitized environment、stdin close、分離pipe、任意のJSONL chunking、
  byte境界、timeout、signal、process group消滅をtestする。
- success、Provider failure、CLI非0、signal、timeout、protocol drift、output limit、
  collection失敗、spawn失敗、cleanup失敗を区別したままにする。
- Provider successはtool successを意味しない。後続Quality Gateだけが使い捨てWorkspaceを
  評価する。
- Replay、Runner、Codex、Workflow、Slice 5Aのfull suiteとの互換性を維持する。

### Slice 5C: Live Antigravity smokeおよびQuality Gate検証

**ステータス: 未設計、実行未承認。**

Slice 5Cは、review済みSlice 5B実装と2回目の明示的な人間の承認後に限って開始する。
`--confirm-live-antigravity`を追加し、新しいexperimentおよびartifact rootを使用し、
最初のsmokeで許可するProvider呼び出しは最大1回とする。runは手動で実行し、
通常testまたはCIへ含めない。また、retry、fallback、resume、continuation、
replacement、subagent delegationを行わない。

承認は、実際の`agy`実行、cache済み認証、network/API access、modelおよびquota使用、
機密情報を含まない厳密なPrompt、Workspace scope、Gate command、timeout、
stop condition、cleanupをそれぞれ対象としなければならない。terminal resultの
successはtool実行を証明しない。使い捨てWorkspaceのQuality Gateを引き続き正とする。

最初の実runでPhase 3またはPhase 4のArtifactを再利用してはならない。

### Slice 5D: Provider比較

成功しreplay可能なSlice 5C vertical smokeの後に限って開始する。

Workflow Spec 2.0へ詰め込んだりExperimentSpec 1.0を変更したりせず、新しいstrictな
Provider comparison Specを使用する。次を固定する。

- 1つのWorkflow
- Task、Fixture、Acceptance、すべてのGate command
- 生成Promptのintentおよびバージョン管理されたProvider adapter
- repetitionおよびseed付きblock order
- Workspace構築
- timeoutおよびstop condition
- 各Providerが許す範囲で可能な限り近づけたnetwork/tool policy

model、tool実装、認証surface、Prompt transport、Agent Harnessは、Provider固有の
treatment componentである。したがって結果はbase-model比較ではなく、Codex systemと
Antigravity systemの比較である。model IDまたはreasoning effort labelが異なる場合は
報告し、同等であると表現してはならない。

Phase 4の原則を再利用する。

- canonical Planを事前登録する。
- sequentialに実行する。
- 1 run = 1 Provider turn = 1 AgentLab Provider callとする。
- retry、fallback、resume、failed-run replacementを行わない。
- append-only Campaignとする。
- 残りのrunを`not_run`として記録するsafe stopを行う。
- offline-only reportとする。
- 欠落usageおよびProvider固有の欠落観測を維持する。
- 最初の小規模実験から、一般的なmodel performanceまたはstatistical significanceを
  主張しない。

## 現在の保守引き継ぎ

Slice 5Aを保守するagent、または別途承認されたSlice 5B実装を準備するagentは、
次を行わなければならない。

1. 編集前に`feature/phase5`、設計commit、remote parity、追跡対象worktreeが
   cleanであることを確認する。
2. `AGENTS.md`、本書、`docs/ROADMAP.md`、
   `docs/CODEX_PROVIDER.md`、`docs/REPLAY_FORMAT.md`、
   `src/agentlab/models.py`、`src/agentlab/capabilities.py`、関連testを読む。
3. 完了済みオフラインSlice 5A契約を維持する。開始条件を解決した後に別途承認を
   得ない限り、Slice 5B codeを実装しない。
4. 既存schemaおよび完了済みPhaseの挙動を維持する。
5. fake executableおよび合成streamだけを使用する。
6. 実際の`agy -p`または別のProvider taskの起動、認証、credentials確認、
   models/quota照会、Prompt規則の緩和を行わず停止する。許可するのは上限付き
   version/help probeだけである。
7. 必須オフライン検証を実行する。
8. 実際に実装された内容に限ってdocumentを更新する。
9. review済みPhase 5 fileだけを`feature/phase5`へcommitおよびpushする。
10. 別途承認なしにPRを作成する、mainへmergeする、Liveを起動する、
    Slice 5C/5Dを開始する、のいずれも行わない。

完了reportには、変更file、test count、残存する未検証能力を記載し、実際の
Antigravity Provider呼び出しとquota使用が0であったことを確認しなければならない。
