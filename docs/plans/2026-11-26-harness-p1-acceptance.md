# Harness P1 跨进程验收 · 2026-11-26

在本地 R2b 提交 `1982cd26` 上，规范第 8 节的五项可靠执行标准及 R2b 的 `/resume` 恢复链路全部通过。验收矩阵和强杀恢复用量取自同一次正式 JUnit 运行。

## 结果与复现命令

| 范围 | 实测结果 | 用时 |
| --- | --- | ---: |
| BASE 完整 Harness | 962 通过 | 184.50 秒 |
| 加入六条用例后的完整 Harness | 968 通过 | 272.46 秒 |
| 完整 Harness 与 packaging metadata | 972 通过，0 失败、0 错误、0 跳过 | 282.46 秒 |
| 上一行中的 P1 跨进程用例 | 6 通过，每条各有一个 `acceptance_evidence` | 88.83 秒（六条 JUnit case 用时之和） |

环境为 WSL Ubuntu 22.04、Python 3.11.15、pytest 9.0.2、SQLAlchemy 2.0.48、asyncpg 0.31.0、FastAPI 0.133.1、uvicorn 0.41.0 和 httpx 0.28.1。PG fixture 在 Docker 中启动一次性的 `postgres:16`，执行迁移，并在各场景间清表。API 由 `python -m harness.api` 启动。各 Worker 和 Scheduler 使用独立 Python 进程与工作目录，共享数据库、策略文件和产物目录。租约为 2 秒，心跳间隔和首次对账宽限期均为 1 秒。超时场景中，对端登记请求后延迟 2 秒，连接器的 HTTP 读超时为 0.7 秒。

在验收分支的仓库根目录，激活已安装 Harness 测试依赖的 WSL Python 环境，确认 Docker 可用后运行下列命令。

```bash
unset DARAOS_HARNESS_TEST_DSN
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PWD/src:$PWD"
python -m pytest tests/harness_runtime tests/test_packaging_metadata.py \
  -q -n0 -o addopts="" -m "integration or not integration" \
  -o junit_family=xunit1 --junitxml=p1-acceptance.xml
```

回放单个场景时，将下方用例名接到 `tests/harness_runtime/integration/test_p1_acceptance.py::` 后面，保留相同的 pytest 选项。下列脚本读取六个场景的观测字段。

```python
import json
from xml.etree import ElementTree

for case in ElementTree.parse("p1-acceptance.xml").iter("testcase"):
    if case.get("classname", "").endswith(".test_p1_acceptance"):
        entry = case.find("properties/property[@name='acceptance_evidence']")
        print(case.get("name"), json.loads(entry.get("value")))
```

本轮共享的 [JUnit XML](../../../../outputs/p1-acceptance-20261126/p1-acceptance.xml) 含全套 972 条结果。导出时将旧 P0 属性中 21 处临时目录前缀改为相对路径，同时替换套件主机名和两个既有 SQL 测试参数里的示例绝对路径。API 路由、动作标识、回执及断言结果均按实测值保留。导出前后已逐条比对六条 P1 属性的 JSON 内容。文件 SHA-256 为 `cae5f36ffaea1c3806c4e14cb0bca630b60f691cc8094ad164667254e5c6b82c`。

## 验收矩阵

| 要求 | 实际观察 | 跨进程用例 |
| --- | --- | --- |
| 强杀后回收并续跑 | HTTP 可读的检查点为 `tools_completed`，保存了首个动作且待执行列表为空。父进程 `kill()` 后退出码 -9，租约到期后 Scheduler 回收 1 个任务。两代 Attempt 的退出原因依次为 `lease_expired`、`succeeded`。`step-1.0` 在动作表中有一行，读连接器实际执行一次，最终草稿 HTTP 200。 | `test_p1_worker_kill_reclaim_and_resume_across_processes` |
| 重复提交与审批 | API 重启前后，同键同内容得到同一任务，再用同键改内容返回冲突。三次 HTTP 依次为 202/202/409。同人批准、重复批准、改为拒绝依次为 200/200/409。数据库事件经 trace 读到一次 `approve`，服务端只收到一次 `/execute`。 | `test_p1_duplicate_submit_and_approval_execute_once` |
| 取消后无新动作 | 待批动作变 `failed`，取消后批准返 409，外部执行 0 次。运行中取消前后模型调用 ID 列表相同，动作表均只有同一条 `files.read`。含未知动作的任务先报告 `pending_reconciliation`，对账后 `done`，取消后的状态事件没有再次入队。 | `test_p1_cancel_running_waiting_and_unknown_cleanup` |
| 旧 Worker 无法覆盖结果 | 领取后暂停旧进程的心跳，回收并由新 Worker 完成。恢复旧进程后，持原租约真实调用 `heartbeat` 与 `commit(cancelled)`，均得到 `StaleLease`。两次 HTTP 读取的终态、`result_ref` 和版本 5 一致。 | `test_p1_stale_worker_cannot_heartbeat_or_commit` |
| 未知副作用不盲重试 | 服务端先登记动作，再让连接器读响应超时。自动路径对账返回 `succeeded`，动作里的回执与服务登记值一致。人工路径先观测到 `unsupported` 和 `needs_manual_confirmation=true`，通过 `/resolve` 得到可读取的裁决回执。每条路径各有 1 次执行、1 次对账，最终均为 `succeeded`。 | `test_p1_unknown_side_effect_reconciles_without_blind_retry` |
| 等待补充输入后恢复 | 第一代进入 `waiting/input`。旧版本与越界输入分别返回 409 version_conflict 与 400 forbidden，任务版本保持不变。提交正确版本后 HTTP 200、状态 `queued`。第二代模型输入出现一次新增引用，真实读取 `supplement.md`，HTTP 产物含本轮运行生成的文件内容。两次验证为 `inconclusive`、`passed`。 | `test_p1_waiting_input_resume_across_processes` |

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
