# P1-R4 交付报告 · 2026-11-26

当前交付基于 `origin/DaraOS` 的 `9aadb65c`。本页末节记录三段提交、55 分钟重放边界、回执核验和最终全量结果。[跳到 Claude 终审复验](#claude-final-r4)

## 早期 R4 交付记录

`feishu.send_message` 已接入 Main。发送前由 Gateway 校验收件人并等待人工审批。七项指定场景全部通过。最终完整 Harness 测试 1011 项通过，含真实 PostgreSQL。相对 BASE 的 `src/` 新增 208 行，上限 300 行。

| 项目 | 记录 |
| --- | --- |
| BASE / 本轮起点 | `581674fd` / `63328f02` |
| 交付分支 | `feat/harness-feishu-send`，提交保存在本地 |
| 原基线 | 968 passed，失败集合为空。[原始输出](p1-R4-baseline.txt) |
| 基线文件校验 | Git blob `65212f1920142fd19856339d5eec067703c58b1f`，与续跑前一致 |
| 本轮测试环境 | WSL Ubuntu 22.04、Python 3.11.15、pytest 9.0.2。测试及子进程仅有回环接口，`FEISHU_TEST_*` 变量数为 0 |

本轮已执行 `git fetch origin` 并确认 `origin/DaraOS` 包含 `4a7abe38`。开发、暂存和提交均在用户指定的 R4 工作树内完成。

## Step 0 真实双发记录

在 WSL 登录 shell（`bash -lc`）中读取 `FEISHU_TEST_APP_ID`、`FEISHU_TEST_APP_SECRET`、`FEISHU_TEST_RECEIVE_ID` 和 `FEISHU_TEST_RECEIVE_ID_TYPE`，四项均非空。收件人类型为 `chat_id`，目标为用户指定的个人测试群。运行时生成一个 `act_` 加 32 位十六进制的标识，共 36 个字符，两次使用同一标识、同一正文和同一收件人。

| 请求 | HTTP | 业务 `code` | 业务 `msg` | 观测 |
| --- | ---: | ---: | --- | --- |
| 获取 `tenant_access_token` | 200 | 0 | `ok` | 取得令牌 |
| 第一次发送 | 200 | 0 | `success` | 有非空回执 |
| 第二次发送 | 200 | 0 | `success` | 有非空回执，与第一次相同 |

两次均已调用 `POST /open-apis/im/v1/messages`。在内存中比较完整的 `data.message_id`，相等结果为 `true`。这满足任务卡对重复请求返回原回执的判据。本次测量覆盖连续两次发送。一小时是任务卡规定的对账窗口。报告仅保留回执存在性和相等性。

以上记录来自 `63328f02`。本轮只对回环地址上的模拟服务发送请求。飞书开放平台没有按 UUID 只读查询消息的接口。窗口内重放可以取回先前的回执，也可以在首个请求未到达时完成首次投递。因此，回执证明这条消息最终已发出，两次连续实测只验证了重复 UUID 返回同一回执。窗口取动作创建时间起的一小时。[飞书发送消息接口](https://open.feishu.cn/document/server-docs/im-v1/message/create.md)

## 改动清单

| 文件 | 交付内容 |
| --- | --- |
| `tools/policy.py`、`tools/gateway.py` | Gateway 先校验输入和路径，再按 `RECIPIENT_ARGS` 提取 `<receive_id_type>:<receive_id>`。部署必须允许；用户配置 `recipients` 时还须命中该列表。已有用户授权省略 `recipients` 时沿用部署范围，显式空列表拒绝所有收件人。 |
| `contracts.py`、`tools/gateway.py` | `ActionRef` 增加带默认值的 `scope_id`、`args_ref`、`created_at`。对账时由 Gateway 传入持久化动作的上下文。 |
| `tools/connectors/feishu.py`、`tools/connectors/__init__.py` | 实现并导出 `FeishuSendConnector`。请求时按变量名取凭据，获取应用令牌，使用已批准的正文和收件人、`uuid=action_id` 发送文本。成功回执进入 `receipt_ref` 与工具摘要。 |
| `config.py`、`app.py`、`agents/main.py` | 装配连接器、Main 工具列表、策略 YAML、部署收件人和正超时。配置的安全表示展示凭据变量名及收件人数，凭据值只在发请求时解析。 |
| `capabilities/feishu_message.yaml`、`prompts/feishu_message.md`、`README.md` | 发布 `feishu_message@1`，说明审批、白名单、恢复和提交回执的实际格式。 |
| `tests/harness_runtime/scenarios/test_feishu_send.py` | 使用回环 HTTP 假服务和脚本模型，覆盖真实 Gateway、审批服务、Recovery、Worker、模型提交协议与 Verifier。 |
| `tests/harness_runtime/unit/test_verifier.py` | 唯一既有测试改动是在 `test_main_profile_registry` 的期望集合加入 `feishu.send_message`。 |

源码文件在表中相对于 `src/harness/`。连接器在对账前按作用域读取原始参数，校对动作的 `request_hash`，并在换取令牌后、发送前再次检查时钟。已到一小时或缺少创建时间时，返回 `unsupported` 并进入人工确认。超时、断连、5xx、限流、消息仍在发送、未知业务码或没有有效回执会保留 `unknown`。确定性拒绝为 `failed`。获取令牌失败时，对账仍保留原发送结果未知。对端原始 `msg` 和响应正文不进入工具错误记录。[飞书消息错误码](https://open.feishu.cn/document/server-docs/im-v1/message/create.md) · [飞书频控说明](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/guide/faq)

## 验收结果

| 指定场景 | 离线实测 |
| --- | --- |
| `test_feishu_send_requires_approval` | 白名单内操作登记为 `needs_approval`，认证与发送请求合计 0 次。 |
| `test_feishu_send_approved_once` | 由原 actor 批准请求哈希后发出 1 次消息 POST。UUID 与动作 ID 相同，回执与假服务器记录一致。再次调用同一逻辑键复用回执，HTTP 计数不变。 |
| `test_feishu_send_timeout_reconcile_same_uuid` | 服务端已受理，客户端等回执超时后动作变为 `unknown`。动作年龄 3599 秒时，Recovery 用同一 UUID、收件人和正文重放。两次消息 POST 只落 1 条消息，持久化原回执并重新入队。另测首次请求未送达，重放后恰有 1 条消息。 |
| `test_feishu_reconcile_after_one_hour_unsupported` | 3600、3601 秒两组均新增 0 次 HTTP；连续两个 tick 只记 1 次 `unsupported`。旧引用缺少时间同样直接转人工。换取令牌期间跨过截止点时，新增消息 POST 为 0。 |
| `test_feishu_recipient_outside_whitelist_denied` | 部署空列表、相同 ID 但类型不同、用户未放行和用户空列表均返回 `denied/forbidden`。登记动作、审批请求、预算预留、HTTP 均为 0。 |
| `test_feishu_credentials_never_leaked` | 使用运行时生成的合成标记，逐项检查成功、超时对账、业务错误和 HTTP 拒绝后的 events、trace、磁盘 artifacts、DEBUG 日志和配置安全表示。即使对端在错误 `msg` 中回显标记，也未检出凭据值。Worker 用例另外检查了送入模型的上下文。 |
| `test_feishu_capability_end_to_end` | 真实 Worker 在审批前等待，批准后完成发送、写回执产物和校验。实际 `submit_result.evidence` 经适配器映射到 `evidence_refs`；直接构造候选结果也可用 `required_action_receipts`。两条路径均验证了成功回执通过、伪造回执失败。 |

独立离线复核还确认了现有 Verifier 的证据边界。给出候选回执时，Verifier 会核对其引用。只写一个可读的 `receipt` 产物、完全省略证据时，通用验收只检查产物，任务可以显示 `succeeded`，此时发送次数仍为 0。判断实际投递应核对本任务成功的 `feishu.send_message` 动作及其 `receipt_ref`。本卡按授权使用现有 Verifier。

```text
outside_allowlist denied forbidden actions=0 approvals_requested=0 open_reservations=0 http=0
candidate_omitting_evidence succeeded checks=required_artifacts,artifact_readable send_requests=0
```

## 先红后绿

先在 `63328f02` 工作树加入七个必测场景、最初的参数化用例和获准的 Main 期望值。此时 `src/` 尚无差异。使用以下定向命令运行后，原配置不接受 `feishu_base_url`，Main 也缺少发送工具。第一轮实现后，同一组 26 项全部转绿。

```bash
python -m pytest tests/harness_runtime/scenarios/test_feishu_send.py tests/harness_runtime/unit/test_verifier.py::test_main_profile_registry -q -n0 -o addopts="" --tb=line
```

<details>
<summary>最初红灯的 pytest 失败用例和汇总</summary>

```text
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_feishu_send_requires_approval
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_feishu_send_approved_once
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_feishu_send_timeout_reconcile_same_uuid
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_feishu_reconcile_after_one_hour_unsupported[3600]
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_feishu_reconcile_after_one_hour_unsupported[3601]
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_feishu_recipient_outside_whitelist_denied[deployment0-grant0]
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_feishu_recipient_outside_whitelist_denied[deployment1-grant1]
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_feishu_recipient_outside_whitelist_denied[deployment2-grant2]
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_feishu_recipient_outside_whitelist_denied[deployment3-grant3]
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_feishu_credentials_never_leaked[success]
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_feishu_credentials_never_leaked[accept_timeout]
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_feishu_credentials_never_leaked[business]
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_feishu_credentials_never_leaked[forbidden]
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_feishu_capability_end_to_end[True]
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_feishu_capability_end_to_end[False]
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_reconciliation_classifies_remote_response[disconnect-unknown]
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_reconciliation_classifies_remote_response[busy-unknown]
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_reconciliation_classifies_remote_response[throttled-unknown]
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_reconciliation_classifies_remote_response[malformed-unknown]
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_reconciliation_classifies_remote_response[no_receipt-unknown]
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_reconciliation_classifies_remote_response[forbidden-failed]
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_reconciliation_classifies_remote_response[business-failed]
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_legacy_action_and_expiry_during_token_request
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_credentials_resolved_at_request_time
FAILED tests/harness_runtime/scenarios/test_feishu_send.py::test_feishu_config_and_policy
FAILED tests/harness_runtime/unit/test_verifier.py::test_main_profile_registry
26 failed in 2.90s
```

</details>

```text
26 passed in 10.98s
```

随后加测飞书在 HTTP 400 中返回群限流码且 `data` 为空对象的情况，首次得到 `failed`。将业务码解析与成功回执解析分开后，临时错误保留 `unknown`。继续覆盖窗口中途跨界、认证失败及提交协议。

```text
E   AssertionError: assert ['failed'] == ['unknown']
1 failed, 38 deselected in 4.24s
42 passed in 16.85s
44 passed in 18.74s
```

## 三项工程检查

### 1. 完整 Harness 测试

使用原基线所用的 pytest 参数，包含所有 integration 用例。每轮创建独立的 `postgres:16` 容器，使用本机已有镜像、`--network=none` 和私有 Unix 套接字。WSL 登录 shell 为每个测试进程清除全部 `FEISHU_TEST_*`、默认飞书生产凭据变量和代理环境变量，`bwrap --unshare-net` 将网络限制到回环。环境检查输出如下。

```text
network=loopback-only; FEISHU_TEST_*=0; PostgreSQL=16/private-socket
```

```bash
python -m pytest tests/harness_runtime -q -n0 -o addopts="" -m "integration or not integration"
```

首轮完整运行收集了 1009 项，`test_dsn_never_shown` 失败。诊断字典的字段名 `feishu_app_secret_env` 含断言检查的小写字符串。改为用 `feishu_credential_env_names` 展示两个变量名，保留了原有断言。首轮脱敏原始输出如下，测试 DSN 已替换，行末空格已清理。

<details>
<summary>首轮完整输出</summary>

```text
........................................................................ [  7%]
........................................................................ [ 14%]
........................................................................ [ 21%]
........................................................................ [ 28%]
........................................................................ [ 35%]
.......................................................................F [ 42%]
........................................................................ [ 49%]
........................................................................ [ 57%]
........................................................................ [ 64%]
........................................................................ [ 71%]
........................................................................ [ 78%]
........................................................................ [ 85%]
........................................................................ [ 92%]
........................................................................ [ 99%]
.                                                                        [100%]
=================================== FAILURES ===================================
_____________________________ test_dsn_never_shown _____________________________

    def test_dsn_never_shown():
        config = HarnessConfig.from_env({P + "DATABASE_DSN": "postgresql://<redacted>"})
>       assert "secret" not in repr(config)
E       assert 'secret' not in "HarnessConf...sec': 10.0})"
E
E         'secret' is contained here:
E           eishu_app_secret_env': 'DARAOS_HARNESS_FEISHU_APP_SECRET', 'feishu_allowed_recipient_count': 0, 'feishu_timeout_sec': 10.0})
E         ?           ++++++

tests/harness_runtime/unit/test_config.py:43: AssertionError
=========================== short test summary info ============================
FAILED tests/harness_runtime/unit/test_config.py::test_dsn_never_shown - asse...
1 failed, 1008 passed in 273.54s (0:04:33)
```

</details>

先独立重跑原失败用例和飞书配置测试，两项通过。扩展后的最终测试集还包括两种回执提交方式的正反例。最终提交内容的完整 pytest 原始输出如下。

```text
........................................................................ [  7%]
........................................................................ [ 14%]
........................................................................ [ 21%]
........................................................................ [ 28%]
........................................................................ [ 35%]
........................................................................ [ 42%]
........................................................................ [ 49%]
........................................................................ [ 56%]
........................................................................ [ 64%]
........................................................................ [ 71%]
........................................................................ [ 78%]
........................................................................ [ 85%]
........................................................................ [ 92%]
........................................................................ [ 99%]
...                                                                      [100%]
1011 passed in 258.72s (0:04:18)
```

最终新增用例 43 个。基线失败集合和最终失败集合均为空，完整运行无跳过。

### 2. 防御性写法

```bash
git diff 581674fd -- 'src/harness/*.py' ':!src/harness/contracts.py' ':!src/harness/interfaces.py' ':!src/harness/storage/repositories.py' \
  | grep -nE '^\+.*(except Exception|except:|getattr\([^)]*,[^)]*,|hasattr\(|self\._\w+ is not None)|^\+\s+def __init__\([^)]*=None|^\+ {8,}(from \.|import )'
```

标准输出为空，`git diff` 退出码 0，`grep` 的无匹配退出码为 1。

```text
```

人工复核的 `= None` 新字段属于任务卡授权的 `UserGrant`、`ActionRef` 和外部响应的 Pydantic 模型。连接器构造参数及服务方法的参数均显式传入。运行时读取原始环境或策略映射，外部成功应答按严格类型解析。

### 3. 源码增量

```bash
git diff --numstat 581674fd -- src/ ':!src/harness/prompts' ':!src/harness/capabilities' ':!src/harness/README.md' \
  | awk '{s+=$1} END {print s}'
```

```text
208
```

逐文件原始 `numstat` 如下。第一列为新增行、第二列为删除行。文档、能力包、提示词和测试按卡片约定单列。

```text
1	1	src/harness/agents/main.py
7	3	src/harness/app.py
27	0	src/harness/config.py
3	0	src/harness/contracts.py
2	0	src/harness/tools/connectors/__init__.py
147	0	src/harness/tools/connectors/feishu.py
10	2	src/harness/tools/gateway.py
11	0	src/harness/tools/policy.py
```

`git diff --check` 通过。此次续跑仅修改授权清单中的文件，原 BASE 基线文件保持相同 blob。`Runner`、`Recovery`、`Verifier`、存储、API 和数据库迁移均沿用原实现。

<a id="claude-final-r4"></a>

## Claude 终审修复与 `9aadb65c` 合并树全量验证

R4 从新的 P1-ACC 提交 `4e6efbbe` 叠加，重放完原 R4 后的起点为 `40e44052`。最终合并树共 1088 项通过，失败、错误、跳过均为 0。历史提交 `63328f02` 记录的白名单、动作引用和既有测试授权阻断已在前轮解除。上文 Step 0 两次响应的观测沿用原记录，本轮飞书公网请求为 0。

### 对账、授权与回执的实测结果

| 项目 | 行为与观测 |
| --- | --- |
| M1、S5 | 首次发送遇到确定性业务拒绝、403 或认证断连/超时均为 `failed`。发送请求中途断连为 `unknown`。对账时，只有 HTTP 200、整数业务码 0 和有效消息 ID 才能确认成功。机器人被移出群、无权限、非法目标、非 JSON 403、限流、未知业务码、空回执或认证中断均保留 `unknown`。拒绝一次重放无法判定先前请求的投递结果。 |
| M2 | 13 组取消、授权和参数变更测试逐项断言连接器调用 0 次、新增 HTTP 0 次。事件记录 `task_cancelled`、`policy_revoked`、`recipient_revoked` 或 `args_unavailable`，自动对账转人工。只读对账仍可清理已取消任务，既有 R3 场景和 P1 跨进程验收均通过。 |
| M4、S6 | 动作年龄 3299 秒时，同一 UUID 两次消息 POST 只落一条消息，返回原回执。3300、3600、3601 秒均直接 `unsupported`，新增 HTTP 0 次。换令牌期间从 3299 秒跨到 3300 秒，仅有新增认证请求，新增消息 POST 0 次。一小时去重窗口预留五分钟，年龄从动作创建时刻计算。审批耗时达到 55 分钟后若首次投递结果未知，直接交给人工确认。 |
| S4 | 非法 JSON、缺少收件人或正文、非对象、字段类型错误均返回 `unsupported`。将损坏任务排在首位，同一次 `Recovery.tick()` 仍能对随后任务取回原回执并将其动作置为 `succeeded`。 |
| M3 | `feishu_message@1` 的结果 Schema 强制要求摘要、`artifact_refs.receipt` 和至少一条回执引用。历史上“仅写可读产物、省略证据”可成功的用例，现在只选 `artifact_readable` 也得到 `failed`。创建任务时在 `acceptance.checks` 中指定 `feishu_receipt`，会进一步逐项核对本任务成功发送动作、候选引用与产物正文。伪造、跨任务回执、未发消息、缺失产物、正文不符均失败。 |
| M3、M6 | 真实 Worker 分别经 `submit_result.evidence` 和 `CandidateResult.required_action_receipts` 提交。有效回执得到 `PASSED/SUCCEEDED`，虚构回执得到 `FAILED/FAILED`。四个组合均观测到生产装配的 `BaselineWriter.write` 原方法经 `after_commit` 返回。 |

独立运行 JSON Schema 校验器的 9 组样本均符合预期，覆盖两种证据字段、摘要和回执缺失或为空、空证据数组及空证据条目。

新增校验器位于 `src/harness/results/checks/feishu.py`，由 `src/harness/app.py` 注册。共享输入模型校验持久化参数并生成网关使用的输入 Schema。网关在再次访问外部系统前按当前工具与收件人授权核对同一份获批参数。本轮修改的源文件、能力包、提示词和测试见下方完整统计。相对 `40e44052`，通用 Verifier、Runner、存储、API、P2 业务文件及 `test_verifier.py` 的差异均为空。

### TDD 与合并树回归

在 WSL 的独立网络命名空间运行 pytest 与子进程，仅有回环接口。使用本地镜像启动 PostgreSQL 16，通过专用 Unix socket 接入；每次测试前清除 `FEISHU_TEST_*`、默认飞书凭据与代理变量。飞书测试使用回环 HTTP 假服务和运行时生成的合成凭据。原 R4 基线文件的 Git blob 仍为 `65212f1920142fd19856339d5eec067703c58b1f`。

| 检查点 | pytest 实测 |
| --- | --- |
| rebase 后，仅含原 R4 的 `40e44052`，完整 Harness + packaging | 1051 passed in 281.41s |
| 只写测试，首次定向红灯 | 79 failed, 1 passed in 14.26s。77 项在创建测试任务时即遇到尚未声明的 `feishu_receipt`。 |
| 将通用夹具恢复为只选 `artifact_readable`，仅在回执专测中指定新检查器，再跑红灯 | 40 failed, 40 passed in 29.01s。实际复现重放被判失败、缺撤权原因、过界重放、参数解析异常、认证断连被判未知及遗漏证据仍可通过。此时业务代码差异为零。 |
| 同组测试，完成实现后 | 80 passed in 29.91s |
| 最终合并树，含 Docker PostgreSQL 和 4 项 packaging metadata | 1088 passed in 292.75s (0:04:52) |

```bash
python -m pytest tests/harness_runtime/scenarios/test_feishu_send.py -q -n0 -o addopts="" --tb=line
python -m pytest tests/harness_runtime tests/test_packaging_metadata.py -q -n0 -o addopts="" -m "integration or not integration"
```

最终全量命令脱敏标准输出如下。

```text
........................................................................ [  6%]
........................................................................ [ 13%]
........................................................................ [ 19%]
........................................................................ [ 26%]
........................................................................ [ 33%]
........................................................................ [ 39%]
........................................................................ [ 46%]
........................................................................ [ 52%]
........................................................................ [ 59%]
........................................................................ [ 66%]
........................................................................ [ 72%]
........................................................................ [ 79%]
........................................................................ [ 86%]
........................................................................ [ 92%]
........................................................................ [ 99%]
........                                                                 [100%]
1088 passed in 292.75s (0:04:52)
```

### 工程检查与本地提交

相对 P1-ACC 的防御性检查中，`git diff` 退出 0，`grep` 退出 1，标准输出为 0 字节。新增源码按任务卡排除 README、prompts 和 capabilities。以下两项实测分别对应本轮新增与 R4 整层累计新增。

```bash
git diff 4e6efbbe -- 'src/harness/*.py' ':!src/harness/contracts.py' ':!src/harness/interfaces.py' ':!src/harness/storage/repositories.py' | grep -nE '^\+.*(except Exception|except:|getattr\([^)]*,[^)]*,|hasattr\(|self\._\w+ is not None)|^\+\s+def __init__\([^)]*=None|^\+ {8,}(from \.|import )'
git diff --numstat 40e44052 -- src/ ':!src/harness/prompts' ':!src/harness/capabilities' ':!src/harness/README.md' | awk '{s+=$1} END {print s+0}'
git diff --numstat 4e6efbbe -- src/ ':!src/harness/prompts' ':!src/harness/capabilities' ':!src/harness/README.md' | awk '{s+=$1} END {print s+0}'
```

```text
98
278
```

R4 本轮新增 98 行，上限 150 行。R2b 本轮新增 53 行，上限 80 行。P1-ACC 与 R2b 的 `src/` 树完全相同。三个阶段的防御性 grep 均无命中，各阶段 `git diff --check` 均通过。主仓工作区保留了开工时已有的一份未跟踪文档。报告、代码、测试和导出的 JUnit 已做脱敏检查。

| 本地分支 | 本轮起点 | 交付提交 | 全量通过 |
| --- | --- | --- | ---: |
| `feat/harness-p1-r2-completion` | `fd343774` | `94a356ff`，已核对干净 | 998 |
| `test/harness-p1-acceptance` | `d594a28e` | `4e6efbbe`，已核对干净 | 1008 |
| `feat/harness-feishu-send` | `40e44052` | 本报告所属提交，可用 `git log -1 --format=%h -- docs/plans/p1-R4-report.md` 定位 | 1088 |

三段本地提交以 `9aadb65c → 94a356ff → 4e6efbbe → feat/harness-feishu-send` 为祖先顺序。R2b 与 P1-ACC 的完整本轮文件统计也已写入各自报告。正式 P1 JUnit 位于 `outputs/p1-acceptance-20261126/p1-acceptance.xml`，SHA-256 为 `fb36d5b518c41468d64d5ec6448b55310621e78372f288ac31e967c7c9014197`。文件含 6 份 P1 跨进程 `acceptance_evidence`。S2 的重新挂起原子化按终审卡暂缓。

本轮完整文件增减统计（相对 `40e44052`）

```text
150	0	docs/plans/p1-R4-report.md
22	5	src/harness/README.md
5	2	src/harness/app.py
2	0	src/harness/capabilities/feishu_message.yaml
20	0	src/harness/capabilities/schemas/feishu_message_result.json
1	1	src/harness/prompts/feishu_message.md
25	0	src/harness/results/checks/feishu.py
39	28	src/harness/tools/connectors/feishu.py
29	0	src/harness/tools/gateway.py
201	18	tests/harness_runtime/scenarios/test_feishu_send.py
```

三卡合并树完整文件增减统计（相对 `9aadb65c`，含原三卡和本轮终审修复）

```text
79	0	docs/plans/2026-11-26-harness-p1-acceptance.md
15	0	docs/plans/p1-ACC-baseline.txt
83	0	docs/plans/p1-ACC-report.md
47	0	docs/plans/p1-R2b-baseline.txt
257	0	docs/plans/p1-R2b-report.md
15	0	docs/plans/p1-R4-baseline.txt
384	0	docs/plans/p1-R4-report.md
95	25	src/harness/README.md
1	1	src/harness/agents/main.py
40	7	src/harness/api/app.py
8	3	src/harness/api/approvals.py
14	5	src/harness/app.py
13	0	src/harness/capabilities/feishu_message.yaml
20	0	src/harness/capabilities/schemas/feishu_message_result.json
32	0	src/harness/config.py
19	3	src/harness/context/resume.py
11	0	src/harness/contracts.py
11	0	src/harness/prompts/feishu_message.md
25	0	src/harness/results/checks/feishu.py
48	10	src/harness/storage/memory.py
62	21	src/harness/storage/postgres.py
29	7	src/harness/storage/repositories.py
7	4	src/harness/tasks/approvals.py
5	0	src/harness/tasks/service.py
2	0	src/harness/tools/connectors/__init__.py
158	0	src/harness/tools/connectors/feishu.py
50	9	src/harness/tools/gateway.py
11	0	src/harness/tools/policy.py
275	0	tests/harness_runtime/integration/_p1_acceptance_helpers.py
373	0	tests/harness_runtime/integration/test_p1_acceptance.py
79	0	tests/harness_runtime/integration/test_r2b_races.py
636	0	tests/harness_runtime/scenarios/test_feishu_send.py
258	0	tests/harness_runtime/scenarios/test_r2b_approvals.py
221	0	tests/harness_runtime/scenarios/test_r2b_resume.py
54	0	tests/harness_runtime/unit/test_approvals.py
10	0	tests/harness_runtime/unit/test_config.py
53	0	tests/harness_runtime/unit/test_r2b_context.py
230	0	tests/harness_runtime/unit/test_r2b_repositories.py
1	1	tests/harness_runtime/unit/test_verifier.py
```

合并树总计为 `39 files changed, 3731 insertions(+), 96 deletions(-)`。

## 2026-11-27 · M3' 强制核验飞书回执

父提交为 `6a1ba076`。本轮在 `feat/harness-feishu-send` 为 `feishu_message@1` 声明 `required_checks: [feishu_receipt]`，同时在任务创建与结果验收时执行这条要求。前节的 1088 项是父提交的回归记录，本轮合并树实测 **1103 项通过，0 失败、0 错误、0 跳过**。

| 验收点 | 实测结果 |
| --- | --- |
| 创建请求漏配检查 | `checks=[]` 或仅含 `artifact_readable` 时，`TaskService.create` 抛出 `InvalidRequest`，`POST /tasks` 返回 HTTP 400 和 `error_code=invalid_request`。沿用同一请求键补入 `feishu_receipt` 后返回 202。 |
| 历史任务绕过创建入口 | 直接构造并存入只声明 `artifact_readable` 的任务。写入任意可读的 `receipt` 产物，再把该产物 ID 作为证据。通用检查均通过，Verifier 补跑的 `feishu_receipt` 失败，整体验收为 `FAILED`。显式声明该检查的有效投递仍通过，核验记录中该项只出现一次。 |
| 能力包兼容性 | `load_pack` 接受缺省及空列表，对合法名称保序去重。非列表、非字符串、空白名称及未列入 `verifiers` 的名称均被拒绝。9 个内置能力包中仅飞书包声明必需检查，原位置参数构造方式仍可用。`_required_actions` 函数与既有 Verifier 单元测试均已对照父提交，内容一致。 |
| 飞书超时与取消清理 | 配置和环境入口均接受 60 秒，拒绝 60.1 和 301 秒，默认 10 秒。回环假服务实测，存在未知投递时取消任务，状态为 `cancelled`、清理为 `pending_reconciliation`。任务所有者调用 `/resolve` 返回 200 后，清理变为 `done`，期间新增飞书 HTTP 请求为 0。README 与 Prompt 已同步。 |

先添加测试，在生产代码与父提交一致时复现了 API 返回 202、历史任务虚构投递仍被判通过，以及超时超过上限仍被接受。测试使用 WSL、仅有回环接口的网络命名空间与真实 Docker PostgreSQL 16.16。测试进程的 `FEISHU_TEST_*` 变量数为 0，真实飞书请求为 0。

```bash
python -m pytest tests/harness_runtime/scenarios/test_feishu_send.py tests/harness_runtime/unit/test_verifier.py tests/harness_runtime/unit/test_task_service.py -q -n0 -o addopts="" --tb=line
python -m pytest tests/harness_runtime tests/test_packaging_metadata.py -q -n0 -o addopts="" -m "integration or not integration"
```

| 运行 | pytest 汇总 |
| --- | --- |
| 定向红灯 | 16 failed, 141 passed in 37.56s |
| 定向绿灯 | 157 passed in 36.62s |
| 完整 Harness、PostgreSQL 集成与 4 项 packaging metadata | 1103 passed in 305.95s (0:05:05) |

`git diff --shortstat 6a1ba076..HEAD` 的精确输出如下。

```text
10 files changed, 170 insertions(+), 25 deletions(-)
```

`git diff --numstat 6a1ba076..HEAD` 的两列数字依次为新增和删除行。

```text
47	0	docs/plans/p1-R4-report.md
13	6	src/harness/README.md
2	0	src/harness/capabilities/feishu_message.yaml
11	2	src/harness/capabilities/registry.py
3	2	src/harness/config.py
1	1	src/harness/prompts/feishu_message.md
7	3	src/harness/results/verifier.py
5	0	src/harness/tasks/service.py
58	11	tests/harness_runtime/scenarios/test_feishu_send.py
23	0	tests/harness_runtime/unit/test_verifier.py
```

其中 4 个 `src/` Python 文件新增 26 行、删除 7 行，低于 45 行上限。对本次差异的新增行执行了四组 grep，分别检查防御性写法、凭据与飞书标识、本机绝对路径和仓外相对链接。四组输出均为 0 字节，`grep` 退出码均为 1。`git diff --check` 通过。
