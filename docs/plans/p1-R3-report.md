# P1 R3 停止报告

日期：2026-11-17。状态：**停止，未实现 R3**。

分支：`feat/harness-p1-reconcile`。BASE：R1 最终提交 `0fd199be267f6de894d0a45d39d081c7a195c7fb`。本分支按要求从该提交新建 worktree，只新增本报告。

## 阻塞原因

任务卡要求 `Recovery.tick(now)` 自动遍历 `waiting/reconciliation` 任务，并继续处理已 cancelled 但仍有未决副作用的任务。但是 R1 完成后的仓库接口无法找出这些任务：

| 现有 / 卡片允许的新接口 | 为什么不能发现任务 |
|---|---|
| `TaskRepository.get(scope_id, task_id)` | 必须先知道任务及 scope |
| `TaskRepository.claim(...)` | 只领取 queued 任务，不能用来扫描 waiting / cancelled |
| `TaskRepository.reclaim_expired(now, max_attempts) -> int` | 只返回回收数量，且扫描 running / cancelling；不能找出先前已等待或已取消的任务 |
| `EventRepository.list(scope_id, ...)` | 必须先知道 scope，且没有 scope 枚举接口 |
| `ActionRepository.list_for_task(task_id)` | 必须先知道任务 |
| R3 允许新增 `ActionRepository.list_unsettled(task_id)` | 仍必须先知道任务 |
| R3 允许新增 `TaskRepository.release_reconciliation(scope_id, task_id, now)` | 只转换指定任务状态，不能发现任务 |

直接读取内存仓库的私有状态、在 Recovery 内查询 PostgreSQL、复用 #26 的 Scheduler，或私自扩大上述方法的签名，都不能满足任务卡和仓库的职责边界。R3 允许改的仓库文件虽然包含所需位置，但“改什么”只列出两个新接口，没有授权新增任务枚举接口。用户要求严格遵守这张表并在需要越界时停止，因此在实施及新增 R3 测试前停止，没有先做部分功能再扩大范围。

继续 R3 所需的最小任务卡补充：明确允许在 `TaskRepository` 及内存 / PostgreSQL 实现增加任务查询方法（例如 `list_reconciliation_tasks()`），返回等待对账和取消后仍需清理的任务；随后用同一套内存 / PostgreSQL 测试验证，计入 R3 的 250 行上限。这里只列缺口，不把该建议视为已获授权。

不需要因本次发现修改 #26 / #23 或增加迁移。用量 `settle/release(call_id, now)` 已能在租约结束后使用；实际阻塞点是待对账任务的发现入口。

## 当前交付状态

- R1 已实现，最终分支提交为上述 BASE；完整测试原始输出见同目录 `p1-R1-report.md`。
- R3 未新增 `TOOL_RECONCILE`、Gateway reconcile/resolve、人工裁决 API、cleanup 字段或 FakeExternalConnector；没有可称作“R3 通过”的测试结果。
- R2：未开始，等待 #26 合入、#23 的契约去留定下来，并等待 R1 的集成条件。
- S1：未开始，等待 R1 与 #26 合入。
- P1 验收：未开始，等待 R1 / R2 / R3 / S1 合入。
- R4：未开始，等待 P1 验收和真实飞书测试凭据。
- 只做本地提交，没有 push、没有开 PR。
- 飞书表格要求 R1、R3 均完成后再写。该前提不成立，因此没有写入飞书，没有修改已有内容和表格，也没有声称完成写入后的读回。

## 四项检查

按通用约定在 R3 worktree 执行四项命令。由于本分支只有报告，测试验证的是继承的 R1 代码，**不代表 R3 已实现或验收**。测试使用 `/home/xue/venvs/harness-pg/bin/python`，PG fixture 自建临时数据库；源码与测试均未调整。

### 1. 完整测试

```bash
python -m pytest tests/harness_runtime -q -n0 -o addopts=""
```

退出码 0。原始输出：

```text
/home/xue/venvs/harness-pg/lib/python3.11/site-packages/pytest_asyncio/plugin.py:306: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to "function" scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session". Set it to "fixture-scope" to keep the current behavior.

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
........................................................................ [ 15%]
........................................................................ [ 30%]
........................................................................ [ 45%]
........................................................................ [ 60%]
........................................................................ [ 75%]
........................................................................ [ 90%]
..............................................                           [100%]
478 passed in 45.39s

```

### 2. 防御性写法检查

```bash
git diff 0fd199be267f6de894d0a45d39d081c7a195c7fb -- src/harness | grep -nE '^\+.*(except Exception|except:|getattr\([^)]*,[^)]*,|hasattr\(|self\._\w+ is not None)|^\+\s+\w+=None,$|^\+.*def [^(]*\(.*=None[,)]|^\+ {8,}(from \.|import )'
```

原始输出为空，grep 退出码 1（无匹配行）：

```text
```

### 3. src/ 新增行数

```bash
git diff --numstat 0fd199be267f6de894d0a45d39d081c7a195c7fb -- src/ | awk '{s+=$1} END {print s}'
```

退出码 0。原始输出为一个空行（卡片的 awk 命令在没有任何输入行时打印空值）；`src/` 没有差异，新增行数为 0。

```text

```

### 4. 与 #26 试合并

```bash
git fetch origin pull/26/head:refs/remotes/origin/pr-26 && git merge-tree --write-tree --name-only origin/pr-26 HEAD
```

#26 head 为 `800d2e1d4a62314b627cc682114067bebb8ebd17`。检查时 HEAD 是本分支 BASE；随后仅增加本报告。退出码 0，无冲突文件；fetch 无输出，merge-tree 原始输出：

```text
ce41093ba2bc020cee554d896748865f82b9598d

```
