# P1-ACC 本地交付报告 · 2026-11-27

本轮以 `test/harness-p1-acceptance` 的 `4e6efbbe` 为父提交。完整 Harness、4 项 packaging metadata 和真实 PostgreSQL 16.16 回归共 **1011 项通过，0 失败、0 错误、0 跳过**。七条 P1 跨进程测试均生成独立证据。[P1 正式验收报告](2026-11-26-harness-p1-acceptance.md)

## 超时和用例拆分

- 仓库级 `tests/conftest.py` 为普通用例设置 30 秒，为带 `integration` 标记的用例设置 90 秒。POSIX 上，`finally` 取消闹钟并恢复原信号处理器。Windows 沿用已有分支。
- `tests/harness_runtime/integration/test_p1_acceptance.py` 将未知副作用的自动和人工路径拆为两个 pytest 参数实例，每条分别启动进程与本地 HTTP 服务，fixture 在每条开始前清理共用的测试库。每条均校验 `/execute` 只发生一次，并保留该路径的审批、超时、对账回执、HTTP 顺序与最终状态断言。
- `tests/harness_runtime/unit/test_conftest_timeout.py` 用真实 pytest marker、模拟信号接口验证 30/90 秒、超时消息以及恢复旧处理器。两条用例在旧 fixture 上失败，修改后均通过。另在独立 POSIX 进程用真实 `SIGALRM` 复核正常返回、测试体抛错和超时三种退出方式，两组时限共六项检查全部通过。

## 七条跨进程验收

| 场景 | 跨进程用例 | JUnit 耗时（秒） | 本轮实测 |
| --- | --- | ---: | --- |
| 强杀恢复 | `test_p1_worker_kill_reclaim_and_resume_across_processes` | 11.638 | 退出 -9，回收 1，检查点 `tools_completed`，动作 `step-1.0` 执行 1 次，最终产物 HTTP 200。模型/工具调用 3/2，总 token 5534。 |
| 提交与审批幂等 | `test_p1_duplicate_submit_and_approval_execute_once` | 10.044 | 提交 HTTP 202/202/409，审批 HTTP 200/200/409，审批决定 1 条，外部执行 1 次。重启前后任务 ID 一致。 |
| 三类取消 | `test_p1_cancel_running_waiting_and_unknown_cleanup` | 17.644 | 待批任务取消后审批 HTTP 409，外部执行 0 次。运行中取消前后各有 1/1 个动作。未知动作清理 `pending_reconciliation → done`，终态 `cancelled`。 |
| 旧租约隔离 | `test_p1_stale_worker_cannot_heartbeat_or_commit` | 11.542 | 旧 Worker 的 heartbeat 和 commit 均得到 `StaleLease`，`committed=false`。新结果版本 5 → 5，产物引用一致。 |
| 未知副作用 · 自动对账 | `test_p1_unknown_side_effect_reconciles_without_blind_retry[auto]` | 14.147 | 首次执行得到 `deadline_exceeded`。对端收到 `/execute` 1 次、`/reconcile` 1 次。对账为 `succeeded`，任务 `succeeded`。 |
| 未知副作用 · 人工裁决 | `test_p1_unknown_side_effect_reconciles_without_blind_retry[manual]` | 13.966 | 首次执行得到 `deadline_exceeded`。对端收到 `/execute` 1 次、`/reconcile` 1 次。裁决 HTTP 200，对账 `unsupported → succeeded`，任务 `succeeded`。 |
| 补充输入恢复 | `test_p1_waiting_input_resume_across_processes` | 7.926 | 旧版本、越界输入分别为 409 version_conflict 和 400 forbidden。合法恢复 HTTP 200，新输入在模型上下文出现 1 次，核验 `inconclusive → passed`。 |

以上合计 86.907 秒。父提交的合并用例曾用时 28.543 秒，距当时的 30 秒上限仅 1.457 秒。本次 `[auto]` 为 14.147 秒，`[manual]` 为 13.966 秒，均独立适用 90 秒上限。

## 全量回归与证据导出

R2b 基线 `94a356ff` 的完整 Harness 为 998 项通过。上一次 `4e6efbbe` 的完整 Harness 与 packaging metadata 为 1008 项通过。最初的开发基线仍保存在仓内。[早期 BASE 输出](p1-ACC-baseline.txt)

测试进程和子进程只使用回环网络，清除了 `FEISHU_TEST_*`。临时 Docker PG 16 经独立 Unix 套接字接入。正式命令如下，正式 XML 暂存于验收工作树的 pytest 忽略目录，脱敏校验后原子替换导出件。首次全量自检为 1011 passed in 263.57s，取件时 WSL 临时目录已不存在。下列时间和证据均来自随后完成导出并回读的正式运行。

```bash
python -m pytest tests/harness_runtime tests/test_packaging_metadata.py \
  -q -n0 -o addopts="" -m "integration or not integration" \
  -o junit_family=xunit1 --junitxml=p1-acceptance.xml
```

```text
1011 passed in 265.77s (0:04:25)
```

脱敏 JUnit 为 `outputs/p1-acceptance-20261126/p1-acceptance.xml`，SHA-256 为 `64fab0967dcc2ec5920b149415842aa2ddc7423747b9c775989e2301db6d1b97`。文件含 1011 个 testcase、13 份 `acceptance_evidence`，其中 P0 六份、P1 七份。导出时替换了 1 处套件主机名、21 处 P0 临时目录前缀和 2 处固有测试参数路径。逐项比对原始与导出 XML，七份 P1 `acceptance_evidence` 原文和七条 `testcase.time` 均完全相同。目标文件读回后，SHA-256 一致。

## 工程检查

`git diff --exit-code 94a356ff -- src/` 标准输出为空，退出码 0，源文件新增、删除均为 0。本次改动限于全局超时 fixture、P1 对账场景、超时回归单测和两份验收报告。`git diff --check 4e6efbbe` 通过。导出件和报告已扫描本机路径、飞书标识及凭据特征，两份报告的文件链接均在仓内。
