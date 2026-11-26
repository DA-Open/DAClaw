# Harness P1 跨进程验收 · 2026-11-26

在本地 R2b 提交 `94a356ff` 上，六个跨进程场景全部通过。验收工作树的 rebase 起点为 `d594a28e`，包含主干 `9aadb65c`。下文所有本轮观测均取自同一次正式 JUnit。

## 结果与复现命令

| 范围 | 实测结果 | 用时 |
| --- | --- | ---: |
| R2b BASE 完整 Harness | 998 通过 | 196.97 秒 |
| `d594a28e` 修改前完整 Harness 与 packaging metadata | 1008 通过 | 294.70 秒 |
| 正式验收，含 4 项 packaging metadata | 1008 通过，0 失败、0 错误、0 跳过 | 279.65 秒 |
| 上一行中的 P1 跨进程用例 | 6 通过，每条各有 1 份 `acceptance_evidence` | 93.53 秒（六条 JUnit case 用时之和） |

环境为 WSL Ubuntu 22.04、Python 3.11.15 和 pytest 9.0.2。测试及其子进程只使用回环接口，`FEISHU_TEST_*` 变量数为 0。一次性 Docker `postgres:16` 禁用网络，通过私有 Unix 套接字提供真实 PostgreSQL。API 以 `python -m harness.api` 启动，各 Worker 和 Scheduler 使用独立进程与工作目录，共享数据库、策略和产物目录。租约为 2 秒，心跳间隔与首次对账宽限期均为 1 秒。对端登记请求后延迟 2.5 秒，连接器的 HTTP 读超时为 1.0 秒。六个场景使用 POSIX 信号和 pipe select，原生 Windows 由模块标记跳过。

从验收工作树运行下列命令。先激活 Harness 的 Linux 环境，将 `DARAOS_HARNESS_TEST_DSN` 指向已启动的专用临时 PostgreSQL Unix 套接字；测试会清理其中的数据。

```bash
set +x
for name in ${!FEISHU_TEST_@}; do unset "$name"; done
unset DARAOS_HARNESS_FEISHU_APP_ID DARAOS_HARNESS_FEISHU_APP_SECRET
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src:$PWD"
bwrap --unshare-net --dev-bind / / -- python -m pytest tests/harness_runtime tests/test_packaging_metadata.py \
  -q -n0 -o addopts="" -m "integration or not integration" \
  -o junit_family=xunit1 --junitxml=p1-acceptance.xml
```

单个场景可按 `tests/harness_runtime/integration/test_p1_acceptance.py::<用例名>` 回放。下面的脚本读取六份观测。

```python
import json
from xml.etree import ElementTree

for case in ElementTree.parse("p1-acceptance.xml").iter("testcase"):
    if case.get("classname", "").endswith(".test_p1_acceptance"):
        item = case.find("properties/property[@name='acceptance_evidence']")
        print(case.get("name"), json.loads(item.get("value")))
```

JUnit 文件保存在 `outputs/p1-acceptance-20261126/p1-acceptance.xml`，SHA-256 为 `fb36d5b518c41468d64d5ec6448b55310621e78372f288ac31e967c7c9014197`。文件包含 1008 条测试结果、12 条 `acceptance_evidence`。导出时替换套件主机名、21 处旧 P0 临时目录前缀和 2 处固有测试参数路径。导出前后逐条比较六条 P1 属性的完整 JSON，内容相同。

## 验收矩阵

| 要求 | JUnit 实际观察 | 跨进程用例 |
| --- | --- | --- |
| 强杀恢复 | 退出 -9，回收 1，检查点 tools_completed，动作 step-1.0 实际执行 1 次。Attempt 退出顺序为 lease_expired → succeeded，产物 HTTP 200。模型/工具调用 3/2，总 token 5534。 | `test_p1_worker_kill_reclaim_and_resume_across_processes` |
| 提交与审批幂等 | 提交 HTTP 202/202/409，审批 HTTP 200/200/409，决定事件 1 条，外部执行 1 次。重启前后任务 ID 相同为 true。 | `test_p1_duplicate_submit_and_approval_execute_once` |
| 三类取消 | 待批取消后批准 HTTP 409，外部执行 0 次。运行中取消前后各有 1/1 个动作；未知动作清理 pending_reconciliation → done，终态 cancelled。 | `test_p1_cancel_running_waiting_and_unknown_cleanup` |
| 旧租约 | 旧 Worker 的 heartbeat、commit 分别得到 StaleLease、StaleLease，committed=false。新结果版本 5 → 5，产物引用一致为 true。 | `test_p1_stale_worker_cannot_heartbeat_or_commit` |
| 未知副作用 | 自动路径 /execute → /reconcile，对账 succeeded。人工路径 /execute → /reconcile，对账 unsupported → succeeded，裁决 HTTP 200。错误码 deadline_exceeded。 | `test_p1_unknown_side_effect_reconciles_without_blind_retry` |
| 补充输入 | 旧版本、越界输入分别为 409 version_conflict、400 forbidden，合法恢复 HTTP 200。新输入在模型上下文出现 1 次，验证 inconclusive → passed，终态 succeeded。 | `test_p1_waiting_input_resume_across_processes` |

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

服务端逐条保留 `/execute` 与 `/reconcile` 的请求体和 `Idempotency-Key`，以真实动作 ID 关联。自动和人工场景使用不同任务，分别计数。人工回执还通过产物接口核对了 `outcome=happened`。

## 局限与范围声明

本轮覆盖 API 与 Worker 重启、进程级强杀、租约隔离及审批和对账的应用层语义。测试固定使用 `ScriptedModelAdapter`。输入补充场景的两代 Worker 共用同一个测试检查器，缺少指定来源时返回 `inconclusive`，补齐后委托内置可读性检查。实际读取的文件和最终草稿另有独立断言。外部服务仅监听本机随机端口，观察范围限于本轮模拟的 HTTP 行为。

PG fixture 关闭了 `fsync`、同步提交和完整页写。机器断电后的数据库持久性、外部供应商的实际幂等行为、在线模型质量以及 Harness 和 packaging 以外的仓库测试，均在本轮观测范围之外。
