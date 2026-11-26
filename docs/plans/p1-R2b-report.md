# P1 R2b 本地交付报告

2026-11-26。R2b 新增的 76 个测试实例全部通过。包含 PostgreSQL 的完整 Harness 共 962 项，961 项通过，唯一失败与开发前的基线一致。按任务卡口径，`src/` 新增 **205 / 260 行**。防御性写法检查输出为空。

| 项目 | 核验结果 |
|---|---|
| 基线 | 在主仓库执行 `git fetch origin` 后，`origin/DaraOS` 精确为 `4a7abe389c9bfed53691660c043fe4505c6f247d` |
| 本地分支 | `feat/harness-p1-r2-completion`，从上述提交创建独立 worktree |
| 测试环境 | WSL Ubuntu 22.04，既有虚拟环境，Python 3.11.15，pytest 9.0.2，SQLAlchemy 2.0.48，asyncpg 0.31.0；测试 fixture 启动 Docker `postgres:16` |
| 完整基线 | 885 通过，1 失败，0 跳过。在写测试和实现前已落盘。[脱敏原始记录](p1-R2b-baseline.txt) |

## 改动清单

生产代码集中在 `src/harness/`。

| 文件 | 改动 |
|---|---|
| `api/app.py`、`tasks/service.py`、`contracts.py` | 新增 `POST /tasks/{id}/resume`、服务方法和 `VersionConflict`。HTTP 先核对 scope、任务所属 actor 和文件白名单，再核对等待原因、版本和合并后的 128 项上限。GET 返回版本与待批动作的六个字段。 |
| `storage/repositories.py`、`storage/memory.py`、`storage/postgres.py` | 补全仓储契约。HTTP 和两端仓储复用同一恢复校验，仓储在持锁后重新核对。新增引用按请求顺序追加，保留历史列表。任务状态、版本与 `resumed_with_input` 事件一次提交。 |
| 同一组仓储文件 | 裁决依次锁定任务、动作及已有审批。有效的相同决定返回已有记录。重新等待时可原位替换已过期的批准，并记录新的裁决和入队事件。取消 queued 或 waiting 任务时，同事务作废其所有待批动作。 |
| `config.py`、`app.py`、`api/approvals.py`、`tasks/approvals.py` | API、服务与 Worker 使用注入的时钟。`DARAOS_HARNESS_APPROVAL_TTL_SEC` 默认为 86400，仅接受正整数。批准可显式指定期限，拒绝保存 `expires_at=None`，API 返回实际到期时间。 |
| `tools/gateway.py` | `approval_requested` 带上 `request_hash`。对账确认失败的副作用在重试时若缺少有效批准，会重新置为 `needs_approval` 并发出请求事件。重放历史拒绝记录时继续执行拒绝决定。 |
| `context/resume.py` | 无检查点时沿用新任务简报。有检查点且没有待执行工具调用时，从已保存的用户消息识别已展示的引用，把其余引用按序补入一条用户消息。存在待执行调用时保留原始消息顺序。 |
| `README.md` | 同步状态表、恢复与裁决路由、GET 字段、TTL、取消语义和 Scheduler 的恢复职责。 |

使用现有 `tasks.input_refs` 和 `0005_approvals`。内存实现使用共享锁，PostgreSQL 使用单事务和行锁。

## 验证覆盖

新增场景通过 ASGI HTTP 客户端访问实际 FastAPI 路由，Worker、Gateway、仓储和事件记录使用项目实现。模型采用 `ScriptedModelAdapter`，外部写入由可计数的测试连接器承接。等待资料的场景注入一个按输入引用判断能否验证的 checker；补齐资料后调用内置 `artifact_readable`，再从 HTTP 下载 Worker 写出的结果核对内容。

| 测试文件（相对 `tests/harness_runtime/`） | 新增实例 | 实测行为 |
|---|---:|---|
| `scenarios/test_r2b_resume.py` | 28 | 内存和 PG 均覆盖去重顺序、事件、版本、权限、路径与请求上限。审批、对账、依赖及非 waiting 状态均拒绝恢复。两个 Worker 在有、无检查点两种情况下读到新增输入，验证事件依次为 `inconclusive` 和 `passed`。 |
| `unit/test_r2b_context.py` | 6 | 短路径与长路径共用前缀、路径含空格或换行、用户已在正文中提过的引用，以及只存在于 system/tool 消息中的引用。另核对 pending tool call 的消息顺序和已有引用全部展示过的情况。 |
| `scenarios/test_r2b_approvals.py` | 20 | HTTP 发现待批动作并读取参数。相同批准连续返回 200，裁决事件只写 1 条，版本只升 1 次，Worker 执行外部工具 1 次。双向改判均为 409。60 秒批准在第 61 秒重放时执行 0 次，重批后执行 1 次并成功。还验证永久拒绝、取消以及对账失败后重新申请审批。 |
| `unit/test_r2b_repositories.py` | 16 | 两个后端用同一组断言验证跨 scope 隔离、有效期边界、审批人、过期批准改为拒绝、取消只作废本任务的待批动作，以及仅有待批动作时的租约回收。 |
| `integration/test_r2b_races.py` | 2 | 两个独立 PostgreSQL 连接池同时恢复同版本任务，仅一个请求入队且仅持久化胜方引用和一条事件。两个连接同时提交相同审批时，返回同一裁决，裁决和入队事件各 1 条。 |
| `unit/test_approvals.py`、`unit/test_config.py` | 4 | Gateway 对 FAILED 动作重挂并补发一次带哈希的事件。服务收到带显式 TTL 的拒绝决定时仍保存空的到期时间，历史拒绝在旧期限届满后继续生效。配置的默认值、覆盖值、脱敏导出和非法值均有断言。 |

仓储测试沿用 `unit/test_repositories.py::repos`。已有审批 HTTP 测试的两个 Stub 增加 `clock` 和 `config`，原断言全部保留。

## 先红后绿

基线落盘后，先在未修改生产代码的 worktree 中运行首批 74 个实例，得到 58 失败、16 通过。已有行为直接通过的实例包括待批动作的租约回收、任务入队后改判返回 409，以及保留待执行工具调用的消息顺序。实现后相同的 74 项全部通过。

首轮失败的原始输出摘录如下。

```text
E   AttributeError: 'InMemoryTaskRepository' object has no attribute 'resume'
E   KeyError: 'version'
E   AssertionError: assert <ActionStatus.FAILED: 'failed'> is <ActionStatus.NEEDS_APPROVAL: 'needs_approval'>
E   AttributeError: 'HarnessConfig' object has no attribute 'approval_ttl_sec'
58 failed, 16 passed in 20.67s
```

```text
74 passed in 21.21s
```

复查时，新增两项双后端版本优先级断言。等待输入的任务已达 128 项上限时，携过期版本提交新引用原先得到 400。将 HTTP 数量检查绑定到仓储将要校验的同一版本后，返回 409 `version_conflict`。另加强了已有检查点用例，覆盖先前用户在普通消息中提到过的引用，修正了该引用被再次追加的问题。两次修正前分别观察到以下结果。

```text
E   assert 400 == 409
2 failed in 7.27s
```

```text
4 failed, 2 passed in 7.13s
```

最终定向复现命令与完整输出如下。

```bash
python -m pytest \
  tests/harness_runtime/scenarios/test_r2b_resume.py \
  tests/harness_runtime/scenarios/test_r2b_approvals.py \
  tests/harness_runtime/unit/test_r2b_context.py \
  tests/harness_runtime/unit/test_r2b_repositories.py \
  tests/harness_runtime/integration/test_r2b_races.py \
  tests/harness_runtime/unit/test_approvals.py::test_retry_failed_side_effect_with_expired_approval_reparks \
  tests/harness_runtime/unit/test_approvals.py::test_rejection_ignores_service_caller_expiry \
  tests/harness_runtime/unit/test_approvals.py::test_replay_honors_preexisting_reject_with_elapsed_expiry \
  tests/harness_runtime/unit/test_config.py::test_approval_ttl_config \
  -q -n0 -o addopts="" -m "integration or not integration" --tb=short
```

```text
........................................................................ [ 94%]
....                                                                     [100%]
76 passed in 21.95s
```

## 提交前的三项检查

### 1. 完整 Harness，含 PostgreSQL

从 worktree 根目录运行，使用其 `src` 作为 Python 模块搜索路径。下方保留完整 pytest 输出，仅替换本机临时路径、进程地址和随机记录 ID。退出码为 1。

```bash
python -m pytest tests/harness_runtime -q -n0 -o addopts="" -m "integration or not integration"
```

```text
........................................................................ [  7%]
........................................................................ [ 14%]
........................................................................ [ 22%]
............................................F........................... [ 29%]
........................................................................ [ 37%]
........................................................................ [ 44%]
........................................................................ [ 52%]
........................................................................ [ 59%]
........................................................................ [ 67%]
........................................................................ [ 74%]
........................................................................ [ 82%]
........................................................................ [ 89%]
........................................................................ [ 97%]
..........................                                               [100%]
=================================== FAILURES ===================================
___________ test_fingerprint_skip_and_diff_after_baseline[postgres] ____________

repos = PgRepositories(tasks=<harness.storage.postgres.PgTaskRepository object at <address>>, events=<harness.storage.pos...bject at <address>>, approvals=<harness.storage.postgres_approvals.PgApprovalRepository object at <address>>)
tmp_path = PosixPath('<pytest-temp>')

    async def test_fingerprint_skip_and_diff_after_baseline(repos, tmp_path):
        path = dataset(tmp_path)
        s = system(tmp_path, repos, model=scripted(path))
        sub = await subscribe(s, path)
        await s.proactive.on_event(event(path))
        assert (await s.worker(lanes=[RunLane.BACKGROUND]).run_once()).status == "succeeded"
        assert await s.proactive.on_event(event(path, "v2", fingerprint="v1")) == []
        assert (await repos.subscriptions.decisions("s1", sub.id))[-1].reason == "unchanged_fingerprint"
        assert len(await s.proactive.on_event(event(path, "v3"))) == 1
        record = (await repos.subscriptions.decisions("s1", sub.id))[-1]
>       assert record.kind == "diff" and record.input_snapshot["previous_version"] == "v1"
E       AssertionError: assert (None == 'diff')
E        +  where None = AdmissionDecision(subscription_id='sub_<id>', event_id='evt_<id>...atetime(2026, 11, 26, 11, 3, 32, 309326, tzinfo=TzInfo(0)), backfill_label=None, backfilled_at=None, label_source=None).kind

tests/harness_runtime/scenarios/test_proactive.py:488: AssertionError
=========================== short test summary info ============================
FAILED tests/harness_runtime/scenarios/test_proactive.py::test_fingerprint_skip_and_diff_after_baseline[postgres]
1 failed, 961 passed in 174.99s (0:02:54)
```

### 2. 防御性写法

```bash
git diff 4a7abe38 -- 'src/harness/*.py' ':!src/harness/contracts.py' ':!src/harness/interfaces.py' ':!src/harness/storage/repositories.py' \
  | grep -nE '^\+.*(except Exception|except:|getattr\([^)]*,[^)]*,|hasattr\(|self\._\w+ is not None)|^\+\s+def __init__\([^)]*=None|^\+ {8,}(from \.|import )'
```

`git diff` 成功，`grep` 退出码为 1，标准输出为 0 字节。以下输出块为空。

```text
```

### 3. 源码新增行数

```bash
git diff --numstat 4a7abe38 -- src/ ':!src/harness/prompts' ':!src/harness/capabilities' ':!src/harness/README.md' | awk '{s+=$1} END {print s}'
```

```text
205
```

该计数包含 Python 模块内新增的注释和 docstring。`src/harness/README.md` 的文档改动单列为 +48 / −21 行。测试、基线、交付报告均在源码计数之外。

## 遗留与交付边界

基线和最终回归的失败集合相同，仅有 `test_fingerprint_skip_and_diff_after_baseline[postgres]`。两次均在取订阅决策列表的最后一项时得到 `kind=None`，断言期望 `diff`。该 PostgreSQL 查询没有指定排序，测试依赖返回顺序。P2 订阅模块属于本卡的禁止修改范围。[失败断言](../../tests/harness_runtime/scenarios/test_proactive.py) · [PostgreSQL 订阅读取](../../src/harness/storage/postgres_proactive.py)

交付文件全部位于 R2b 允许清单。主仓库工作区保留原状，交付方式为独立分支本地提交。任务卡规定的停止条件均未触发。

## Claude 终审修复与 `9aadb65c` 复验

R2b 从 `1982cd26` 无冲突 rebase 到 `origin/DaraOS` 的 `9aadb65c`，得到本轮增量起点 `fd343774`。上文记录的指纹排序问题已由主干 #37、#39 修复。PostgreSQL 现在按决策时间及事件序号排序。本轮基线与最终回归均覆盖了原先偶发失败的用例。[排序实现](../../src/harness/storage/postgres_proactive.py)

| 项目 | 实现与观测 |
| --- | --- |
| M5、S3 | `request_cancel`、`commit`、`revoke_subscription`、`reclaim_expired` 在落入 `cancelled` 时，于同一把内存锁或同一 PostgreSQL 事务内把本任务的待批动作改为 `failed`，同步更新时间。状态事件仅在确有作废动作时携带排序后的 `voided_action_ids`。覆盖相邻任务及五种其他动作状态。 |
| M5，HTTP | `GET /tasks/{id}` 只在 `waiting/approval` 查询并展示待批项。批准入队后，即使动作仍是 `needs_approval`，列表也为空。 |
| S1 | 合并后最多 128 条输入引用。内存和 PostgreSQL 都在仓储校验状态、版本、去重后的数量。超限返回 `InvalidRequest`，任务及事件不变。HTTP 的请求体长度约束复用同一常量。 |
| S8 | WSL 的 pytest 及子进程只有回环网络，`FEISHU_TEST_*` 环境变量数为 0。Docker 使用禁用网络的本地 PostgreSQL 16，通过私有 Unix 套接字连接。 |

`fd343774` 修改前的完整 Harness 基线为 `988 passed in 185.71s (0:03:05)`。下列红灯均在添加断言后、修改 `src/` 前取得。旧实现保留了其他取消路径上的待批动作，批准入队后仍展示待批项，直接调用仓储也接受了 129 条引用。定向复跑结果紧随失败列表。

```text
FAILED tests/harness_runtime/unit/test_r2b_repositories.py::test_cancel_only_voids_this_tasks_pending_actions[memory-waiting]
FAILED tests/harness_runtime/unit/test_r2b_repositories.py::test_cancel_only_voids_this_tasks_pending_actions[memory-queued]
FAILED tests/harness_runtime/unit/test_r2b_repositories.py::test_cancel_only_voids_this_tasks_pending_actions[postgres-waiting]
FAILED tests/harness_runtime/unit/test_r2b_repositories.py::test_cancel_only_voids_this_tasks_pending_actions[postgres-queued]
FAILED tests/harness_runtime/unit/test_r2b_repositories.py::test_running_cancel_commit_voids_pending_approval[memory]
FAILED tests/harness_runtime/unit/test_r2b_repositories.py::test_running_cancel_commit_voids_pending_approval[postgres]
FAILED tests/harness_runtime/unit/test_r2b_repositories.py::test_revoke_subscription_voids_pending_approval[memory]
FAILED tests/harness_runtime/unit/test_r2b_repositories.py::test_revoke_subscription_voids_pending_approval[postgres]
FAILED tests/harness_runtime/unit/test_r2b_repositories.py::test_reclaim_cancelling_voids_pending_approval[memory]
FAILED tests/harness_runtime/unit/test_r2b_repositories.py::test_reclaim_cancelling_voids_pending_approval[postgres]
FAILED tests/harness_runtime/unit/test_r2b_repositories.py::test_resume_repo_enforces_max_input_refs[memory]
FAILED tests/harness_runtime/unit/test_r2b_repositories.py::test_resume_repo_enforces_max_input_refs[postgres]
FAILED tests/harness_runtime/scenarios/test_r2b_approvals.py::test_pending_approvals_only_shown_while_waiting_for_approval[memory]
FAILED tests/harness_runtime/scenarios/test_r2b_approvals.py::test_pending_approvals_only_shown_while_waiting_for_approval[postgres]
14 failed, 4 passed, 28 deselected in 5.09s
..................                                                       [100%]
18 passed, 28 deselected in 5.96s
```

完整回归命令与最终原始输出如下。

```bash
python -m pytest tests/harness_runtime -q -n0 -o addopts="" -m "integration or not integration"
```

```text
........................................................................ [  7%]
........................................................................ [ 14%]
........................................................................ [ 21%]
........................................................................ [ 28%]
........................................................................ [ 36%]
........................................................................ [ 43%]
........................................................................ [ 50%]
........................................................................ [ 57%]
........................................................................ [ 64%]
........................................................................ [ 72%]
........................................................................ [ 79%]
........................................................................ [ 86%]
........................................................................ [ 93%]
..............................................................           [100%]
998 passed in 196.97s (0:03:16)
```

防御性检查相对 `9aadb65c`，增量相对 `fd343774`。`git diff` 成功，`grep` 返回 1，标准输出为空。源码新增 53 行，上限为 80 行。

```bash
git diff 9aadb65c -- 'src/harness/*.py' ':!src/harness/contracts.py' ':!src/harness/interfaces.py' ':!src/harness/storage/repositories.py' | grep -nE '^\+.*(except Exception|except:|getattr\([^)]*,[^)]*,|hasattr\(|self\._\w+ is not None)|^\+\s+def __init__\([^)]*=None|^\+ {8,}(from \.|import )'
git diff --numstat fd343774 -- src/ ':!src/harness/prompts' ':!src/harness/capabilities' ':!src/harness/README.md' | awk '{s+=$1} END {print s+0}'
```

```text
53
```

本阶段收尾时，R2b 的交付提交由本分支 `git log -1 -- docs/plans/p1-R2b-report.md` 定位。P1-ACC 和 R4 两个工作树仍分别位于 `581674fd`、`8159be78`，均干净；后续阶段以新 R2b 为基点。最终三段提交号随总交付提供。

本轮完整文件增减统计（相对 `fd343774`）

```text
84	0	docs/plans/p1-R2b-report.md
8	1	src/harness/README.md
5	6	src/harness/api/app.py
4	0	src/harness/contracts.py
19	8	src/harness/storage/memory.py
18	8	src/harness/storage/postgres.py
7	1	src/harness/storage/repositories.py
19	1	tests/harness_runtime/scenarios/test_r2b_approvals.py
80	3	tests/harness_runtime/unit/test_r2b_repositories.py
```
