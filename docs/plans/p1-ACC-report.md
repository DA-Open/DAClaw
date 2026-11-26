# P1-ACC 本地交付报告 · 2026-11-26

正式验收 1008 项通过，0 失败、0 错误、0 跳过，含全部 4 项 packaging metadata 及六个真实跨进程 P1 场景。上游 R2b 为 `94a356ff`，已包含 `origin/DaraOS` 的 `9aadb65c`。本阶段的 rebase 起点为 `d594a28e`。相对新 R2b，`src/` 新增和删除均为 0。

## 本轮改动

- `tests/harness_runtime/integration/test_p1_acceptance.py` 在 Windows 上标明 POSIX 信号和 pipe select 依赖，Linux 正式运行执行了全部六条。
- `tests/harness_runtime/integration/_p1_acceptance_helpers.py` 使用 1.0 秒读超时；对端收到并登记请求后等待 2.5 秒，稳定触发客户端读超时。
- 本报告与 [P1 正式验收报告](2026-11-26-harness-p1-acceptance.md) 同步替换两处指向仓库外的 Markdown 链接，保留可复制的相对路径与下列校验和。

JUnit 文件为 `outputs/p1-acceptance-20261126/p1-acceptance.xml`，SHA-256 为 `fb36d5b518c41468d64d5ec6448b55310621e78372f288ac31e967c7c9014197`。共 1008 个 testcase，P0 与 P1 合计 12 份 `acceptance_evidence`。脱敏时改写 21 处临时目录前缀、2 处固有测试参数路径及套件主机名。六份 P1 证据在原始和导出 XML 中的 JSON 内容相同，再读回目标文件计算散列也一致。

## 基线、红灯和回归

最初开发六条场景时，旧 R2b `1982cd26` 的基线为 962 项通过。在补齐跨进程辅助模块前，pytest 以 `ModuleNotFoundError` 报告 `_p1_acceptance_helpers` 缺失；完成后六条最初定向测试均通过。原始基线继续保留。[最初的 BASE 输出](p1-ACC-baseline.txt)

上游完成终审修复后，`94a356ff` 完整 Harness 为 998 项通过，`d594a28e` 的完整 Harness 与 packaging 基线为 `1008 passed in 294.70s (0:04:54)`。本次 S7 的离线检查读取测试标记和超时调用参数，变更前断言失败，修改后通过。

```text
POSIX-only marker=False; read_timeout=0.7; delayed_response=2
POSIX-only marker=True; read_timeout=1.0; delayed_response=2.5
```

在仅有回环网络、已清除 `FEISHU_TEST_*` 的 WSL 测试进程中执行正式验收，共用临时 PostgreSQL 16。pytest 参数如下。

```bash
python -m pytest tests/harness_runtime tests/test_packaging_metadata.py \
  -q -n0 -o addopts="" -m "integration or not integration" \
  -o junit_family=xunit1 --junitxml=p1-acceptance.xml
```

```text
........................................................................ [  7%]
........................................................................ [ 14%]
........................................................................ [ 21%]
........................................................................ [ 28%]
........................................................................ [ 35%]
........................................................................ [ 42%]
........................................................................ [ 50%]
........................................................................ [ 57%]
........................................................................ [ 64%]
........................................................................ [ 71%]
........................................................................ [ 78%]
........................................................................ [ 85%]
........................................................................ [ 92%]
........................................................................ [100%]
1008 passed in 279.65s (0:04:39)
```

## 同一份 JUnit 的六项观测

| 场景 | 实测 |
| --- | --- |
| 强杀恢复 | 退出 -9，回收 1，检查点 tools_completed，动作 step-1.0 实际执行 1 次。Attempt 退出顺序为 lease_expired → succeeded，产物 HTTP 200。模型/工具调用 3/2，总 token 5534。 |
| 提交与审批幂等 | 提交 HTTP 202/202/409，审批 HTTP 200/200/409，决定事件 1 条，外部执行 1 次。重启前后任务 ID 相同为 true。 |
| 三类取消 | 待批取消后批准 HTTP 409，外部执行 0 次。运行中取消前后各有 1/1 个动作；未知动作清理 pending_reconciliation → done，终态 cancelled。 |
| 旧租约 | 旧 Worker 的 heartbeat、commit 分别得到 StaleLease、StaleLease，committed=false。新结果版本 5 → 5，产物引用一致为 true。 |
| 未知副作用 | 自动路径 /execute → /reconcile，对账 succeeded。人工路径 /execute → /reconcile，对账 unsupported → succeeded，裁决 HTTP 200。错误码 deadline_exceeded。 |
| 补充输入 | 旧版本、越界输入分别为 409 version_conflict、400 forbidden，合法恢复 HTTP 200。新输入在模型上下文出现 1 次，验证 inconclusive → passed，终态 succeeded。 |

## 工程检查

相对 `94a356ff` 的防御性 grep 标准输出为 0 字节，`git diff` 退出 0，`grep` 退出 1。

```bash
git diff 94a356ff -- 'src/harness/*.py' ':!src/harness/contracts.py' ':!src/harness/interfaces.py' ':!src/harness/storage/repositories.py' | grep -nE '^\+.*(except Exception|except:|getattr\([^)]*,[^)]*,|hasattr\(|self\._\w+ is not None)|^\+\s+def __init__\([^)]*=None|^\+ {8,}(from \.|import )'
git diff --numstat 94a356ff -- src/ | awk '{s+=$1} END {print s+0}'
```

```text
0
```

`git diff --exit-code 94a356ff -- src/` 输出为空，退出 0。Phase 1 工作树干净且指向 `94a356ff`。本阶段交付提交可用 `git log -1 -- docs/plans/p1-ACC-report.md` 定位。R4 工作树此时干净，仍位于 `8159be78`。本轮增量只涉及上列两份测试和两份报告。

本轮完整文件增减统计（相对 `d594a28e`）

```text
23	22	docs/plans/2026-11-26-harness-p1-acceptance.md
46	104	docs/plans/p1-ACC-report.md
2	2	tests/harness_runtime/integration/_p1_acceptance_helpers.py
5	1	tests/harness_runtime/integration/test_p1_acceptance.py
```
