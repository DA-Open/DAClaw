# P1 R3 实施与停止报告

日期：2026-11-18。状态：**未完成；核心实现已落地，因可复现的并发裁决覆盖问题停止**。

第一次执行（11-17）因缺少待对账任务发现接口停止。11-18 任务卡补充 `TaskRepository.list_reconciliation_candidates()`、Recovery 必传依赖及两项测试后，本次在原 worktree / `feat/harness-p1-reconcile` 的 `260cac06` 上继续。BASE 仍为 R1 最终提交 `0fd199be267f6de894d0a45d39d081c7a195c7fb`。没有新建分支或 rebase。

## 已实现的部分

- TaskRepository 查询所有 scope 中的 waiting/reconciliation 任务及仍有未决副作用的 cancelled 任务，按 scope/id 排序。PG 为单条无锁查询；不加索引或迁移。
- `list_unsettled` 仅返回非只读且 planned / running / unknown 的动作。`release_reconciliation` 在任务行锁内重查等待条件和未决动作，全部有结果才回 queued；已取消的任务不重新入队。
- Gateway 增加 reconcile / resolve：对账只调用 Connector.reconcile；四种结果分别处理；成功结算预留、失败释放预留；人工裁决保存凭据。事件记录 action_id、status、attempt、actor，历史按 seq 分页读取。
- Recovery 从事件历史计算逐动作退避，unknown 最多自动对账 5 次，unsupported 转人工。取消任务继续清理。
- API 新增人工裁决入口；从认证取得身份，任务本人以外返回 403，只读或已确定动作返回 409，跨 scope / 不属于任务的动作返回 404。GET 返回未决动作、人工确认提示和取消清理状态。
- FakeExternalConnector 提供成功、接受后超时、拒绝三种行为及 execute/reconcile 计数。所有 R3 对账场景检查 execute 调用为 0。
- 更新原 R1 测试中 Recovery 的构造调用，补齐必传依赖；没有修改原断言。

`src/` 新增 224 行（上限 250），没有修改 Runner、ToolGateway.invoke、schema、迁移、Scheduler、#26 或 #23 的业务逻辑。

## 新阻塞：人工裁决被迟到的对账结果覆盖

确定性回归：`tests/harness_runtime/scenarios/test_p1_reconcile.py::test_late_reconciliation_cannot_overwrite_manual_decision`，在内存和 PostgreSQL 上各运行一次。

1. 自动对账进入 Connector.reconcile，用事件屏障暂停返回。
2. 本人通过 resolve API 提交 happened，HTTP 200；动作已为 succeeded，任务已 queued。
3. 放行自动对账，Connector 返回 failed。
4. Gateway 的现有无条件 `actions.update` 把已成功动作改为 failed。回归断言要求保留 succeeded，两个实现都失败。

这不是超时或环境失败。未修改的 Gateway.invoke 将 failed 视为可重试动作；R2 尚未实现，所以本次测试没有实际再次执行外部工具，但错误的持久状态会把已经人工确认发生的副作用重新放入可重试路径。不能据先前绿色测试将此实现认定为完成。

当前接口无法原子裁决：

- `ActionRepository.update` 按 action ID 无条件写入，没有预期状态/决策版本条件。
- 动作更新、Usage settle/release、Event append 是各自独立的事务。
- `release_reconciliation(scope_id, task_id, now)` 的行锁仅覆盖该方法，既不接收动作裁决，也不能阻止返回之后迟到的 Gateway 再次覆盖动作。
- 再读一次状态仍有检查后写入的竞态；进程内锁不覆盖 API / Recovery 的不同进程；持有存储事务等待外部 Connector 又违反 README 的职责规则。

需要任务卡明确允许一个**原子裁决提交操作**（或扩展现有写入契约）：在存储锁内拒绝过期结果，协调动作状态、对应用量结算和对账事件，内存与 PG 使用同一组并发回归。当前“允许改 / 改什么”仅列出三个仓库方法，未授权这项写入契约变更。依照用户“补充的接口如果还不够用，同样停下来写报告”的要求，本次停止进一步实现，没有自行扩展接口、加迁移或用重读/进程锁伪装解决。是否仍能满足 250 行上限，需要获准具体契约后再评估，未自行提高上限。

## 测试先行与证据范围

各部分先写测试再改对应实现：仓库首次 18 项因缺少接口失败；Gateway 首次因缺少 reconciliation_history 导入失败；API/Recovery 首次 34 项因构造函数缺少 gateway 参数报错。随后定向测试分别通过，加入并发回归前完整测试输出为：

```text
544 passed in 62.28s (0:01:02)
```

并发复核随后新增上述回归，内存和 PG 均失败；断言保留，未跳过或放宽。下文第一项是包含这两个失败用例的最终完整测试输出，是本次交付状态依据。

所有本次 pytest 命令使用 WSL Ubuntu-22.04 的 `/home/xue/venvs/harness-pg/bin/python`；未用挂载盘的 DAClaw/.venv。PG fixture 自建临时 PostgreSQL 数据库。pytest-asyncio 的现有弃用提示原样保留。

## 飞书与后续阶段

已只读获取飞书开发记录，revision 137，155 个顶层块、6 张原生表格、3 个嵌入表格。读取快照保存在 `E:\Desktop\研0\outputs\p1-20261118\feishu-current-read.json` 和 `feishu-current-outline.json`。

由于 R3 未完成，尚未满足写入 P1 表格的前提。本次没有对飞书作写入，也没有写入后的读回结果。原内容和原表格未修改。不能把只读快照当作写入读回证据。

R1 仍为 `feat/harness-p1-resume` / `0fd199be`。R2、S1、P1 验收、R4 未开始。R1、R3 完成后一起 rebase 到 DaraOS 并开 PR 的后续阶段尚未启动；本次只保留本地提交，没有 push 或开 PR。

## 四项检查原始输出

以下检查的 BASE 均为 `0fd199be`。它们不支持“R3 已完成”的结论；第一项明确失败。

代码 / 回归测试本地提交：`245eac4879108d794e174da4f7293ba588e729a5`（WIP，明确保留失败回归）。报告在后续文档提交中更新，不改变被测代码。

### 1. 完整测试

```bash
python -m pytest tests/harness_runtime -q -n0 -o addopts=""
```

解释器：`/home/xue/venvs/harness-pg/bin/python`。退出码 1。原始输出：

```text
/home/xue/venvs/harness-pg/lib/python3.11/site-packages/pytest_asyncio/plugin.py:306: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to "function" scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session". Set it to "fixture-scope" to keep the current behavior.

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
........................................................................ [ 13%]
..............................................FF........................ [ 26%]
........................................................................ [ 39%]
........................................................................ [ 52%]
........................................................................ [ 65%]
........................................................................ [ 79%]
........................................................................ [ 92%]
..........................................                               [100%]
=================================== FAILURES ===================================
______ test_late_reconciliation_cannot_overwrite_manual_decision[memory] _______

case = namespace(system=HarnessSystem(config=HarnessConfig({'database_dsn': None, 'artifact_root': '/tmp/pytest-of-xue/pytest...f40f9743', resolve='/tasks/task_1eb6a3ffd6ca4848927fdccef40f9743/actions/act_53b1223f8dc04dcb87c7bd26e0bb784c/resolve')
monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x740c66b194d0>

    async def test_late_reconciliation_cannot_overwrite_manual_decision(case, monkeypatch):
        entered, finish = asyncio.Event(), asyncio.Event()
        reconcile = case.connector.reconcile
        case.connector.reconcile_result = ReconcileResult(status="failed")
    
        async def delayed(action):
            entered.set()
            await finish.wait()
            return await reconcile(action)
    
        monkeypatch.setattr(case.connector, "reconcile", delayed)
        automatic = asyncio.create_task(case.recovery.tick(case.clock()))
        await entered.wait()
        try:
            response = await case.client.post(case.resolve, headers=AUTH, json={"outcome": "happened"})
            assert response.status_code == 200, response.text
            assert (await stored_action(case)).status is ActionStatus.SUCCEEDED
            assert (await case.repos.tasks.get("s1", case.task.id)).status is TaskStatus.QUEUED
        finally:
            finish.set()
            await automatic
>       assert (await stored_action(case)).status is ActionStatus.SUCCEEDED
E       AssertionError: assert <ActionStatus.FAILED: 'failed'> is <ActionStatus.SUCCEEDED: 'succeeded'>
E        +  where <ActionStatus.FAILED: 'failed'> = Action(id='act_53b1223f8dc04dcb87c7bd26e0bb784c', task_id='task_1eb6a3ffd6ca4848927fdccef40f9743', attempt_id='att_579...3, 9, 0, tzinfo=datetime.timezone.utc), updated_at=datetime.datetime(2026, 11, 13, 9, 0, tzinfo=datetime.timezone.utc)).status
E        +  and   <ActionStatus.SUCCEEDED: 'succeeded'> = ActionStatus.SUCCEEDED

tests/harness_runtime/scenarios/test_p1_reconcile.py:218: AssertionError
_____ test_late_reconciliation_cannot_overwrite_manual_decision[postgres] ______

case = namespace(system=HarnessSystem(config=HarnessConfig({'database_dsn': None, 'artifact_root': '/tmp/pytest-of-xue/pytest...94749fd2', resolve='/tasks/task_f19befd6ee394f0cb966aa4b94749fd2/actions/act_2fd2391628224115a51c1024c2ba6f81/resolve')
monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x740c66887150>

    async def test_late_reconciliation_cannot_overwrite_manual_decision(case, monkeypatch):
        entered, finish = asyncio.Event(), asyncio.Event()
        reconcile = case.connector.reconcile
        case.connector.reconcile_result = ReconcileResult(status="failed")
    
        async def delayed(action):
            entered.set()
            await finish.wait()
            return await reconcile(action)
    
        monkeypatch.setattr(case.connector, "reconcile", delayed)
        automatic = asyncio.create_task(case.recovery.tick(case.clock()))
        await entered.wait()
        try:
            response = await case.client.post(case.resolve, headers=AUTH, json={"outcome": "happened"})
            assert response.status_code == 200, response.text
            assert (await stored_action(case)).status is ActionStatus.SUCCEEDED
            assert (await case.repos.tasks.get("s1", case.task.id)).status is TaskStatus.QUEUED
        finally:
            finish.set()
            await automatic
>       assert (await stored_action(case)).status is ActionStatus.SUCCEEDED
E       AssertionError: assert <ActionStatus.FAILED: 'failed'> is <ActionStatus.SUCCEEDED: 'succeeded'>
E        +  where <ActionStatus.FAILED: 'failed'> = Action(id='act_2fd2391628224115a51c1024c2ba6f81', task_id='task_f19befd6ee394f0cb966aa4b94749fd2', attempt_id='att_d4b...3, 9, 0, tzinfo=datetime.timezone.utc), updated_at=datetime.datetime(2026, 11, 13, 9, 0, tzinfo=datetime.timezone.utc)).status
E        +  and   <ActionStatus.SUCCEEDED: 'succeeded'> = ActionStatus.SUCCEEDED

tests/harness_runtime/scenarios/test_p1_reconcile.py:218: AssertionError
=========================== short test summary info ============================
FAILED tests/harness_runtime/scenarios/test_p1_reconcile.py::test_late_reconciliation_cannot_overwrite_manual_decision[memory]
FAILED tests/harness_runtime/scenarios/test_p1_reconcile.py::test_late_reconciliation_cannot_overwrite_manual_decision[postgres]
2 failed, 544 passed in 70.71s (0:01:10)

```

### 2. 防御性写法检查

```bash
git diff 0fd199be -- src/harness | grep -nE '^\+.*(except Exception|except:|getattr\([^)]*,[^)]*,|hasattr\(|self\._\w+ is not None)|^\+\s+\w+=None,$|^\+.*def [^(]*\(.*=None[,)]|^\+ {8,}(from \.|import )'
```

使用 Git for Windows 自带 grep，表达式相同。原始输出为空，退出码 1（无匹配）：

```text
```

### 3. src/ 新增行数

```bash
git diff --numstat 0fd199be -- src/ | awk '{s+=$1} END {print s}'
```

新文件已暂存，计入新增 API 文件；使用 Git for Windows 自带 awk。退出码 0，原始输出：

```text
224
```

### 4. 与 #26 试合并

```bash
git fetch origin pull/26/head:refs/remotes/origin/pr-26 && git merge-tree --write-tree --name-only origin/pr-26 HEAD
```

#26 head：`800d2e1d4a62314b627cc682114067bebb8ebd17`。提交前对已暂存 tree 的候选 commit `11b61480333a0cfc83418dc1ae391d3e13030221` 试合并，随后在代码实际提交 HEAD 上执行以上命令；两次退出码均为 0、输出相同，无冲突文件。fetch 无输出，merge-tree 原始输出：

```text
67755df2eadb7e78cb9eba4dd6d44bde897f1ddf

```

源代码和测试的 `git diff --check` 无输出。报告保留失败堆栈的原始行尾空格，文档 diff 中相关空白提示属于原始输出。

原始文件另存 `E:\Desktop\研0\outputs\p1-20261118\`：`R3-api-recovery-red.txt`、`R3-api-recovery-focused.txt`、`R3-pytest-full.txt`（新增并发回归前）、`R3-decision-race.txt`、`R3-pytest-final.txt`（最终）、`R3-merge-head.txt`。
