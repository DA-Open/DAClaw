# Harness P1 跨进程验收 · 2026-11-27 复验

本轮以 P1-ACC 提交 `4e6efbbe` 为父提交。七条 P1 跨进程用例全部通过，耗时和下述观测均取自同一次正式 JUnit。相对上游 R2b `94a356ff`，`src/` 差异为零。

## 结果与复现命令

| 范围 | 实测结果 | 用时 |
| --- | --- | ---: |
| R2b 基线 `94a356ff`，完整 Harness | 998 通过 | 196.97 秒 |
| 上一轮 P1-ACC `4e6efbbe`，完整 Harness 与 packaging metadata | 1008 通过 | 279.65 秒 |
| 本轮正式验收，含 4 项 packaging metadata | 1011 通过，0 失败、0 错误、0 跳过 | 265.77 秒 |
| 上一行中的 P1 跨进程用例 | 7 通过，每条各有 1 份独立证据 | 86.907 秒（七个 testcase 耗时之和） |

普通测试的 `SIGALRM` 上限为 30 秒，带 `integration` 标记的用例为 90 秒。上一轮自动和人工对账在一条用例内串行执行，实测 28.543 秒。本轮拆为 `[auto]` 和 `[manual]` 两条，分别用时 **14.147 秒**和 **13.966 秒**。

使用 WSL Ubuntu 22.04、Python 3.11.15、pytest 9.0.2。测试及子进程只有回环接口，`FEISHU_TEST_*` 变量数为 0。临时 Docker PostgreSQL 16.16 禁用容器网络，经私有 Unix 套接字访问。API 以 `python -m harness.api` 启动，Worker 和 Scheduler 在独立进程与工作目录运行，共享数据库、策略和产物。租约 2 秒，心跳间隔与首次对账宽限期均为 1 秒。外部假服务登记请求后延迟 2.5 秒，连接器读超时 1.0 秒。原生 Windows 沿用 POSIX 信号和 pipe select 的跳过标记。

从验收工作树运行下列命令。先激活 Harness 的 Linux 环境，将 `DARAOS_HARNESS_TEST_DSN` 指向已启动的专用临时 PostgreSQL Unix 套接字。测试会清理其中的数据。

```bash
set +x
for name in ${!FEISHU_TEST_@}; do unset "$name"; done
unset DARAOS_HARNESS_FEISHU_APP_ID DARAOS_HARNESS_FEISHU_APP_SECRET
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src:$PWD"
bwrap --unshare-net --dev-bind / / -- python -m pytest tests/harness_runtime tests/test_packaging_metadata.py \
  -q -n0 -o addopts="" -m "integration or not integration" \
  -o junit_family=xunit1 --junitxml=p1-acceptance.xml
```

单个场景可按 `tests/harness_runtime/integration/test_p1_acceptance.py::<用例名>` 回放。下面的脚本读取七份观测。

```python
import json
from xml.etree import ElementTree

for case in ElementTree.parse("p1-acceptance.xml").iter("testcase"):
    if case.get("classname", "").endswith(".test_p1_acceptance"):
        item = case.find("properties/property[@name='acceptance_evidence']")
        print(case.get("name"), json.loads(item.get("value")))
```

脱敏 JUnit 为 `outputs/p1-acceptance-20261126/p1-acceptance.xml`，SHA-256 为 `64fab0967dcc2ec5920b149415842aa2ddc7423747b9c775989e2301db6d1b97`。文件含 1011 个 testcase、13 份 `acceptance_evidence`，其中 P0 六份、P1 七份。导出时替换了 1 处套件主机名、21 处 P0 临时目录前缀和 2 处固有测试参数路径。逐项比对原始与导出 XML，七份 P1 `acceptance_evidence` 原文和七条 `testcase.time` 均完全相同。

## 七条跨进程用例的耗时与证据

| 场景 | 跨进程用例 | JUnit 耗时（秒） | 本轮实测 |
| --- | --- | ---: | --- |
| 强杀恢复 | `test_p1_worker_kill_reclaim_and_resume_across_processes` | 11.638 | 退出 -9，回收 1，检查点 `tools_completed`，动作 `step-1.0` 执行 1 次，最终产物 HTTP 200。模型/工具调用 3/2，总 token 5534。 |
| 提交与审批幂等 | `test_p1_duplicate_submit_and_approval_execute_once` | 10.044 | 提交 HTTP 202/202/409，审批 HTTP 200/200/409，审批决定 1 条，外部执行 1 次。重启前后任务 ID 一致。 |
| 三类取消 | `test_p1_cancel_running_waiting_and_unknown_cleanup` | 17.644 | 待批任务取消后审批 HTTP 409，外部执行 0 次。运行中取消前后各有 1/1 个动作。未知动作清理 `pending_reconciliation → done`，终态 `cancelled`。 |
| 旧租约隔离 | `test_p1_stale_worker_cannot_heartbeat_or_commit` | 11.542 | 旧 Worker 的 heartbeat 和 commit 均得到 `StaleLease`，`committed=false`。新结果版本 5 → 5，产物引用一致。 |
| 未知副作用 · 自动对账 | `test_p1_unknown_side_effect_reconciles_without_blind_retry[auto]` | 14.147 | 首次执行得到 `deadline_exceeded`。对端收到 `/execute` 1 次、`/reconcile` 1 次。对账为 `succeeded`，任务 `succeeded`。 |
| 未知副作用 · 人工裁决 | `test_p1_unknown_side_effect_reconciles_without_blind_retry[manual]` | 13.966 | 首次执行得到 `deadline_exceeded`。对端收到 `/execute` 1 次、`/reconcile` 1 次。裁决 HTTP 200，对账 `unsupported → succeeded`，任务 `succeeded`。 |
| 补充输入恢复 | `test_p1_waiting_input_resume_across_processes` | 7.926 | 旧版本、越界输入分别为 409 version_conflict 和 400 forbidden。合法恢复 HTTP 200，新输入在模型上下文出现 1 次，核验 `inconclusive → passed`。 |

## 实测输入、输出与错误分类

提交使用 `document_draft@1`，初始输入为 `brief.md`，要求交付可读的 `draft` 且包含 `Goal`、`Plan` 两个标题。强杀恢复场景里，输入原文 `Read this brief and write a project draft.` 出现在最终 HTTP 草稿中。下表将 PG 按 Attempt 结算的用量与 trace 内同一 Attempt 的模型和工具事件逐项核对，再与任务接口汇总值比对。

| 强杀恢复用量 | 模型调用 | 工具调用 | 输入 token | 输出 token |
| --- | ---: | ---: | ---: | ---: |
| 第一代，已执行 `files.read` | 1 | 1 | 1660 | 20 |
| 第二代，写草稿并提交 | 2 | 1 | 3814 | 40 |
| `GET /tasks/{id}` | 3 | 2 | 5474 | 60 |

任务接口的总 token 为 5534。本地 HTTP 服务另行记录读连接器的动作 ID、逻辑键及文件名，覆盖两个 Worker 的执行次数。恢复时的证据同时包含已提交的检查点、独立服务的观测以及最终产物。

| 实际输入或操作 | 接口或持久化结果 | 最终观测 |
| --- | --- | --- |
| 同一请求键改任务正文，或已批准后改判拒绝 | 两项均为 HTTP 409 | 外部服务共执行一次，重复审批未追加决定事件 |
| 运行中取消，或在待批任务取消后尝试批准 | 前者 `running → cancelling → cancelled`，后者批准 HTTP 409 | 前者只保留取消前的已知动作，后者外部执行次数为零 |
| 外部执行已接收、响应超时 | 动作 `unknown`，工具错误 `deadline_exceeded`，任务 `waiting/reconciliation` | `auto` 观测到 `succeeded`。`manual` 观测到 `unsupported → succeeded`，人工请求 HTTP 200 |
| 原租约续期和提交 | 两个调用分别抛出 `StaleLease` | 旧 Worker 的 `committed=false`，新 Worker 的产物仍可读取 |
| 错误版本与白名单外的补件 | HTTP 409 `version_conflict`、HTTP 400 `forbidden` | 以原版本补交允许目录中的新文件后成功 |

服务端逐条保留 `/execute` 与 `/reconcile` 的请求体和 `Idempotency-Key`，以真实动作 ID 关联。自动与人工对账分别运行，使用独立的任务及假服务，各自校验只有一次 `/execute`。人工回执还通过产物接口核对了 `outcome=happened`。

## 局限与范围声明

本轮覆盖 API 与 Worker 重启、进程级强杀、租约隔离及审批和对账的应用层语义。测试固定使用 `ScriptedModelAdapter`。输入补充场景的两代 Worker 共用同一个测试检查器，缺少指定来源时返回 `inconclusive`，补齐后委托内置可读性检查。实际读取的文件和最终草稿另有独立断言。外部服务仅监听本机随机端口，观察范围限于本轮模拟的 HTTP 行为。

PG fixture 关闭了 `fsync`、同步提交和完整页写。机器断电后的数据库持久性、外部供应商的实际幂等行为、在线模型质量以及 Harness 和 packaging 以外的仓库测试，均在本轮观测范围之外。
