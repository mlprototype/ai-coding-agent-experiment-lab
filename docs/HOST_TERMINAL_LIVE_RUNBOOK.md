# Host Terminal Live Campaign Runbook

このRunbookは、AI Coding Agent Experiment Labで人間が個別承認済みのLive Campaignを
Mac Terminalから1回だけ実行し、保存Artifactを次のread-onlyレビューへ引き渡すための
運用手順である。ここにある例だけではLive承認にならない。実行時は必ず、そのCampaignの
Supplemental Approval Packetと人間の明示的なLive承認を正本とする。

## 運用境界

Codex agent内からnested `codex exec`を起動した際にpermission failureが観測された。
詳細なOS-level root causeは未確定であり、「Codex CLIは常にsandboxから実行不能」とは
一般化しない。一方、Mac Terminalで明示的な絶対`CODEX_HOME`を設定した同一shellから実行した
Java Campaignは成功した。

現行運用ではLive CampaignだけをHost Terminalへ分離する。offline preparation、validation、
Report、Public Suite preparation／publication、監査は、各作業のApproval境界に従ってCodex側で
実施できる。sandbox制約を回避する実装は行わない。将来Codex側からLiveを再許可する場合は、
別ApprovalでProvider callを伴うhealth checkの要否を判断し、CLI、認証、child environment、
Artifact、Provider-call budgetの契約を再検証する。

## Approval境界

Live実行前に、少なくとも次が人間レビュー済みでなければならない。

- branch、reviewed HEAD、upstreamが固定されたWorkflow Spec
- strict／canonical load済みのPlan
- statusがpendingで、Spec／Plan／Campaign pathを束縛したSupplemental Approval Packet
- exact Provider-call budgetとrun順序
- 未作成のcreate-only Campaign／Recording／Evidence出力先
- 固定入力のbytes／SHA-256
- quotaと認証状態の人間確認
- 対象Campaign／Provider／Gateの残存processがないことの確認
- 当該Packetだけに対する人間の個別Live承認

次はそれぞれ独立したApproval単位であり、Live承認から自動的に許可されない。

- Live Campaign実行
- Report-only生成
- Public Suite input preparation
- Public Suite publication
- releaseの最終Acceptance
- language／Phase status更新

preparationやpending Packetの生成はLive承認ではない。過去Campaignの失敗や未完了状態も、
新Campaignのretry／resume権限を与えない。

## `CODEX_HOME`契約

利用者は、認証済みCodex homeの既存directoryを絶対pathで明示的にexportする。公開文書、
Approval Packet、Artifactへ個人固有pathを固定しない。

```sh
export CODEX_HOME="/absolute/path/to/existing/codex-home"
```

次のsnippetは値を表示せず、export済み、絶対path、既存directory、readable、searchableを
確認する。成功時は`CONTRACT_PASS`、それ以外は`CONTRACT_FAIL`だけを出力する。

```sh
codex_home_contract=CONTRACT_FAIL
if /usr/bin/env | /usr/bin/grep -q '^CODEX_HOME='; then
  case "${CODEX_HOME-}" in
    /*)
      if [ -d "$CODEX_HOME" ] && [ -r "$CODEX_HOME" ] && [ -x "$CODEX_HOME" ]; then
        codex_home_contract=CONTRACT_PASS
      fi
      ;;
  esac
fi
printf '%s\n' "$codex_home_contract"
unset codex_home_contract
```

- 未設定時に`$HOME/.codex`へ暗黙fallbackしない。
- auth fileをcopy、parse、表示しない。
- API key、cookie、Token、認証対象の識別情報を表示・保存しない。
- `CODEX_HOME`の値をCampaign、Recording、Evidence、Diagnostic、Reportへ保存しない。
- Gate subprocessへ`CODEX_HOME`を渡さない。

## Host Terminal preflight

以下は順序を変えずに実施する。placeholderはSupplemental Approval Packetに記録された値で
置き換える。個人固有path、Campaign ID、model、SHA-256をこのRunbookから推測しない。

### 1. Repositoryへ移動

```sh
export REPOSITORY_ROOT="/absolute/path/to/repository"
cd "$REPOSITORY_ROOT" || exit 1
```

### 2. Git identityとworktree

```sh
export APPROVED_BRANCH="packet-bound-branch"
export APPROVED_HEAD="packet-bound-full-commit"

test "$(git rev-parse --abbrev-ref HEAD)" = "$APPROVED_BRANCH" || exit 1
test "$(git rev-parse HEAD)" = "$APPROVED_HEAD" || exit 1
test "$(git rev-parse '@{upstream}')" = "$APPROVED_HEAD" || exit 1
test "$(git rev-list --left-right --count HEAD...'@{upstream}')" = "0	0" || exit 1
test -z "$(git status --porcelain --untracked-files=no)" || exit 1
```

Git fetch、branch変更、commit、stash、clean、resetはpreflightに含めない。期待値と違えばLiveを
開始せず、人間レビューへ戻す。

### 3. Packet-bound入力のbytes／SHA-256

Packetから各入力のpath、bytes、SHA-256を設定し、Spec、Plan、Acceptance、Manifest、metadata
などPacketが列挙する全fileに同じ確認を行う。

```sh
verify_packet_file() {
  packet_path=$1
  packet_bytes=$2
  packet_sha256=$3
  [ -f "$packet_path" ] && [ ! -L "$packet_path" ] || return 1
  [ "$(wc -c < "$packet_path" | tr -d ' ')" = "$packet_bytes" ] || return 1
  [ "$(shasum -a 256 "$packet_path" | awk '{print $1}')" = "$packet_sha256" ] || return 1
}

verify_packet_file "$SPEC_PATH" "$SPEC_BYTES" "$SPEC_SHA256" || exit 1
verify_packet_file "$PLAN_PATH" "$PLAN_BYTES" "$PLAN_SHA256" || exit 1
verify_packet_file "$SUPPLEMENTAL_APPROVAL_PATH" "$PACKET_BYTES" "$PACKET_SHA256" || exit 1
```

### 4. create-only出力の不存在

Packetに予約されたCampaign、Recording、Evidence、Diagnostic、Report pathをすべて列挙する。
broken symlinkもcollisionとして扱う。

```sh
for packet_output in \
  "$CAMPAIGN_PATH" \
  "$RECORDING_PATH_1" \
  "$EVIDENCE_PATH_1"
do
  if [ -e "$packet_output" ] || [ -L "$packet_output" ]; then
    printf '%s\n' OUTPUT_COLLISION
    exit 1
  fi
done
```

既存出力を削除、rename、上書きして続行しない。

### 5. `CODEX_HOME`契約

前節のsnippetを同じTerminal shellで実行し、出力が`CONTRACT_PASS`であることを人間が確認する。
失敗時はLiveを開始しない。

### 6–9. CLI identity、version、hash、login

```sh
CODEX_BIN=$(command -v codex) || exit 1
test -n "$CODEX_BIN" || exit 1

codex --version
shasum -a 256 "$CODEX_BIN"
codex login status
```

versionとexecutable SHA-256はPacketの許可値へ照合する。`codex login status`は、明示した
`CODEX_HOME`がexportされた同じshellで実行し、認証済みであることだけを人間が確認する。
認証fileや`CODEX_HOME`値を表示せず、出力をArtifactへ保存しない。

`command -v`、version、help、login statusはpreflight用のread-only確認である。これらと、
Prompt送信を伴う実Provider health checkを混同しない。health checkはProvider callであり、
そのcall数を含む別Approvalなしには実行しない。

### 10. 残存process

別のHost Terminalから、対象repositoryに関係するCampaignとProvider processがないことを確認する。

```sh
pgrep -lf '[a]gentlab run-phase6-campaign|[c]odex exec'
process_check_exit=$?
case "$process_check_exit" in
  0) printf '%s\n' ACTIVE_LIVE_PROCESS_FOUND; exit 1 ;;
  1) printf '%s\n' NO_ACTIVE_LIVE_PROCESS ;;
  *) printf '%s\n' PROCESS_CHECK_FAILED; exit 1 ;;
esac
unset process_check_exit
```

OS権限制約で一覧を取得できない場合は成功と推測せず、人間が利用可能なHost側手段で確認し、
その確認をpreflight証跡として扱う。

## Live実行

実際のargvはSupplemental Approval Packetの`exact_argv`をbyte単位で確認し、そのまま使用する。
次は形を示すplaceholderであり、実commandの正本ではない。

```sh
.venv/bin/agentlab run-phase6-campaign \
  <packet-bound-spec> \
  --plan <packet-bound-plan> \
  --campaign <packet-bound-create-only-campaign> \
  --repository-root . \
  --confirm-live-codex \
  --confirm-provider-calls <exact-approved-budget>
```

- model、reasoning effort、workflow順、Provider、call数を手作業で変更しない。
- `CODEX_HOME`を設定・検証した同じMac Terminal shellから実行する。
- Campaign commandは1回だけ送信する。
- retry、fallback、resume、run単位の再実行を行わない。
- 出力がしばらくないことだけを停止やhangと判断しない。shell promptが戻るまで待つ。
- 状態確認が必要なら、別Terminalからread-only process確認だけを行う。
- Enterの連打、同じcommandの再送信、Ctrl-Cを行わない。
- 中断が必要な異常時は同一Campaignを再実行せず、生成済みArtifactとprocess状態を固定して
  人間判断へ戻す。

## Post-run

shell commandのexit codeとCampaign outcomeは別の契約である。exit 0でも個別runのProvider、
Gate、Harness、cleanupが失敗している可能性がある。成功・失敗にかかわらず、次をread-onlyで
確認する。

- Campaign、Recording、Evidenceを正式loaderでstrict／canonical loadする。
- Campaign event順、stop reason、attempted／completed／failed／interrupted／not-runを照合する。
- Provider calls、call-count unknown runs、retry／fallback／resumeをPlanとPacketへ照合する。
- Recording／Evidence、Spec／Plan／Fixture／Acceptance／toolchainのbindingを照合する。
- Provider process group、Gate process、workspace、temporary rootのcleanupを確認する。
- raw Prompt、raw Provider output、raw stderr、agent message、reasoningを表示・永続化しない。
- Provider accountingへ確定値、または確定不能部分を含む範囲を反映する。

同一Campaignは再実行しない。Report-only生成、Public Suite preparation／publication、status更新は
それぞれ別Approvalを待つ。

## Failure handling

| Failure | 扱い |
|---|---|
| Preflight failure | Provider前に停止し、drift／collisionをread-only調査する |
| `CODEX_HOME` contract failure | fallbackせず停止し、Host環境を人間が確認する |
| Provider environment construction failure | Harness failureとして保存し、Provider起動を推測しない |
| Provider CLI nonzero | safe metadataで分類し、raw stderrを公開しない |
| Harness failure | 品質不合格へ変換せず、failure stageとlifecycleを監査する |
| Gate failure | Provider成功と区別し、実行済みGate Evidenceを保持する |
| Cleanup failure | 他の結果より優先し、残存process／workspaceを人間が確認する |
| User interruption | call数を確定できなければunknownとし、中断Artifactを保持する |

どのfailureでも、同一Campaignのretry／fallback／resumeや手動Artifact修復へ進まない。Artifactを
そのまま保存し、追加実行なしのread-only root-cause investigationと新しい人間判断へ戻る。
