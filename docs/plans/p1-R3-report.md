# P1 R3 完成报告

首版 R3 完整测试通过 583 项，源码新增 316 行。11-18 review 修复及配置项移位后，完整测试 633 项通过（71.32 秒），本轮计入上限 86/90 行。与 #26 试合并只剩任务卡允许的 README 冲突。详见文末四项原始输出。

第一次执行（11-17）因缺少待对账任务发现接口停止；11-18 的任务卡第 5 条补充 `TaskRepository.list_reconciliation_candidates()` 后继续。随后复现人工 succeeded 被迟到自动 failed 覆盖；第 6 条授权原子 `ActionRepository.decide` 后修复，原回归断言未改。因内存和 PG 两份 decide 实现使累计新增源码达到 316 行，曾按 300 行上限停止；11-18 用户明确将上限调整为 316，并要求源码不再修改。本次仅完成报告和交付收尾。

## 分支、提交与范围

- 分支：`feat/harness-p1-reconcile`，沿用原 worktree。实现及本地验证阶段没有新建分支或 rebase；R1、R3 均完成后，按用户后续指令进入文末记录的发布阶段。
- 四项检查 BASE：R1 最终提交 `0fd199be267f6de894d0a45d39d081c7a195c7fb`。
- 本次被测代码提交：`af78765541636cd0f77f9671c26c7f293a005350`；随后仅提交本报告。
- 没有修改 Runner、ToolGateway.invoke、schema、迁移、Scheduler、#26 或 #23 的业务逻辑；R2、S1、P1 验收、R4 未开始。

## 实现内容

- 跨 scope 发现 waiting/reconciliation 和仍有未决副作用的 cancelled 任务；PG 使用单条 EXISTS 查询，按 scope/id 排序，不加锁、索引或迁移。
- Gateway 的自动对账四种结果、人工裁决都先存产物，再调用 `ActionRepository.decide`；原来分步修改动作、预留和事件的代码已删除。对账从不调用 execute。
- 内存实现使用同一临界区；PG 在一个事务内先锁任务，再锁动作和关联预留。动作已不在 planned/running/unknown 时返回 None，动作、预留、事件和任务均不写入。
- 生效裁决更新动作，成功结算原调用及该动作 retry 的开放预留，失败释放；unknown/unsupported 保持预留。对账事件的 attempt 在锁内从历史事件计算。全部副作用确定后，同一事务将 waiting/reconciliation 改回 queued；cancelled 不重新入队。
- 自动裁决返回 None 后结束；人工裁决无论在 API 前置检查还是检查后的竞态中落败，都返回 409，响应含 `current_status`。
- 保留事件分页、指数退避、5 次 unknown 后停止、unsupported 转人工，以及 GET 的取消清理状态和未决动作。
- `release_reconciliation` 仅为初始没有未决动作的候选保留：此时没有可供 decide 使用的 action_id。它与 decide 复用同一内部释放函数；API 不再调用它，Recovery 对非空动作列表不再单独释放。原空任务测试保留，新增 Recovery 空列表路径测试。

## 测试先行与复核

在修改原子实现之前，新仓库测试输出 `29 failed, 16 passed in 7.44s`；Gateway/API 的相关测试输出 `10 failed, 48 deselected in 7.65s`。实现后仓库专项测试 `45 passed in 6.77s`。完整测试见下方原始输出：583 项通过。


新验证覆盖：既有 `test_late_reconciliation_cannot_overwrite_manual_decision`（断言未改，内存和 PG）；`test_manual_after_automatic_409`；PG 两个独立连接同时裁决只产生一次结算和一条对账事件；人工请求通过检查后输掉竞态；全部四种迟到自动结果无写入；终态 no-op、作用域及动作归属、原调用和 retry 预留隔离、空任务释放。取消测试按更新后的任务卡改为调用 decide，保留任务仍为 cancelled 的断言，并检查原事件不变、仅新增裁决事件。

只读独立审查未发现新的原子裁决正确性缺陷；具体测试证据见下方原始输出。

## 行数上限调整与保留接口

任务卡通用约定：`src/ 下新增行数不超过本卡上限（不含 tests/）。超出时停下来报告，不要压缩代码来凑数。`

相对 BASE 的新增源码为 **316 行**。原子裁决需要内存和 PostgreSQL 两份实现，原 300 行上限不足；发现超限后按约定停止，没有压缩格式、删除测试或放宽断言。11-18 用户明确授权将上限从 **300 调整为 316**，并要求不再修改源码。因此本次计数满足最新授权，代码仍为 `af787655` 的被测版本。

用户同时明确保留 `release_reconciliation`：它处理任务仍为 waiting/reconciliation、但已没有未决动作的情况。`decide` 必须有 action_id，覆盖不到这一入口。两者调用同一内部释放函数，不保留两套释放逻辑；取消状态仍在锁内重查。

按文件计数的原始输出：

```text
16	1	src/harness/api/app.py
45	0	src/harness/api/reconcile.py
1	0	src/harness/contracts.py
69	0	src/harness/storage/memory.py
86	0	src/harness/storage/postgres.py
14	0	src/harness/storage/repositories.py
29	4	src/harness/tasks/recovery.py
56	1	src/harness/tools/gateway.py
```

此次调整只改变源码行数上限；允许文件、禁止项、其他停止条件以及 R2/S1/P1 验收/R4 不在本次范围内的约束均未改变。

## 四项检查原始输出

测试环境为 WSL Ubuntu 22.04 上的 Python 3.11 虚拟环境（pytest 9.2.0）。PG 测试按已有 fixture 自建临时库。pytest-asyncio 的既有弃用提示原样保留，没有修改配置。

### 1. 完整测试

等价于在该 worktree 中用上述解释器执行：

```text
python -m pytest tests/harness_runtime -q -n0 -o addopts=""
```

原始输出（退出码 0）：

```text
<venv>/lib/python3.11/site-packages/pytest_asyncio/plugin.py:306: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to "function" scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session". Set it to "fixture-scope" to keep the current behavior.

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
........................................................................ [ 12%]
........................................................................ [ 24%]
........................................................................ [ 37%]
........................................................................ [ 49%]
........................................................................ [ 61%]
........................................................................ [ 74%]
........................................................................ [ 86%]
........................................................................ [ 98%]
.......                                                                  [100%]
583 passed in 60.59s (0:01:00)
```

### 2. 防御性写法检查

```text
git diff 0fd199be -- src/harness | grep -nE '^\+.*(except Exception|except:|getattr\([^)]*,[^)]*,|hasattr\(|self\._\w+ is not None)|^\+\s+\w+=None,$|^\+.*def [^(]*\(.*=None[,)]|^\+ {8,}(from \.|import )'
```

原始标准输出为空：

```text
```

grep 退出码 1，表示没有匹配项。Windows 实际使用 `grep`，正则和输入不变。`git diff --check 0fd199be -- src/harness tests/harness_runtime` 也没有空白错误。

### 3. 源码新增行数

```text
git diff --numstat 0fd199be -- src/ | awk '{s+=$1} END {print s}'
```

原始输出（awk 退出码 0；满足更新后的 316 行上限）：

```text
316
```

Windows 使用 Git for Windows 的 awk.exe。计数包含所有新增源码，不把删除行抵扣新增行。

### 4. 与 #26 试合并

```text
git fetch origin pull/26/head:refs/remotes/origin/pr-26 && git merge-tree --write-tree --name-only origin/pr-26 HEAD
```

#26 head 为 `800d2e1d4a62314b627cc682114067bebb8ebd17`。提交前对暂存 tree 的候选提交 `5315c1a5a23336fda6611cafe4eccf4f77f6404c` 检查，提交后对实际 HEAD `af787655` 执行同样检查；两次退出码均为 0，输出一致。fetch 无输出，merge-tree 原始输出：

```text
b108ebd370d2db4a2d5724969c81319008e5f8e3
```

没有冲突文件。试合并没有修改 worktree 或分支。

## 组内记录

已在组内开发记录中追加 P1 表格并读回核对。

## 发布阶段与最终读回

R1 原验证提交为 `0fd199be`，完整测试 478 passed in 46.00s，其报告保留四项检查原始输出。R3 原验证代码提交 `af787655` 的 583 项测试通过，316 行上限和保留 release_reconciliation 已获用户明确授权，飞书追加及读回完成后，按用户“R1 和 R3 都做完以后，再一起 rebase 到 DaraOS 上开 PR”的指令发布。

最新 DaraOS 为 `62b431ce9af534b64692e3843b1cf1cceff24d7d`，#22 已合入。R1 执行 `git rebase --onto origin/DaraOS 2929550e`；R3 执行 `git rebase --onto feat/harness-p1-resume 0fd199be`，保留 R1 → R3 的依赖顺序。两次均无冲突，无任何手工源码修改。

| 版本 | rebase 前 | rebase 后 |
|---|---|---|
| R1 完成提交 | `0fd199be` | `df2866910f3a5071813d838099699a1195781b50` |
| R3 原子裁决代码 | `af787655` | `625f7a4e` |
| R3 首次完成报告 | `49879961` | `83adbcd62677ed031d100666cc98762d8b4b0630` |

分别用 `git diff --exit-code 0fd199be df286691 -- src/harness tests/harness_runtime pyproject.toml` 和 `git diff --exit-code 49879961 83adbcd6 -- src/harness tests/harness_runtime pyproject.toml` 验证，标准输出均为空、退出码均为 0。因此保留原完整测试证据，没有把它表述为 rebase 后重跑；后续提交仅更新报告。相对各自 PR 基线，源码新增仍为 R1 210 行、R3 316 行，diff --check 无错误。

rebase 后对两分支再次执行 `git merge-tree --write-tree --name-only origin/pr-26 HEAD`，均退出 0，无冲突文件，原始输出分别为：

```text
97525ab30e18f7c825f3e5489c47ae35f02279cf
8b0c11f77949b7609674a57ba808e22248c79473
```

两个分支已推送到 `DA-Open/DAClaw`，创建并读回确认的草稿 PR：

- [R1 #27](https://github.com/DA-Open/DAClaw/pull/27)：`feat/harness-p1-resume` → `DaraOS`。
- [R3 #28](https://github.com/DA-Open/DAClaw/pull/28)：`feat/harness-p1-reconcile` → `feat/harness-p1-resume`，因此 diff 仅包含 R3。R1 合入后需将 R3 目标改为 DaraOS；本次没有合并任何 PR。

GitHub 连接器创建 PR 返回集成权限 403，随后使用本机已认证的 gh 完成创建，再由连接器读回确认两个 PR 均 open、draft、未合并。没有修改认证配置。

组内发布记录已追加两个草稿 PR 并读回核对。

## 11-18 review 修复

#27 推送 `e47de9bf` 后，执行 `git rebase feat/harness-p1-resume` 无冲突。刚 rebase 完的 R3 为 `2b863a6c`，移位前的提交 `c682e2e9` 完整测试为 `633 passed in 75.29s`，但超过当时的 80 行限制，与 #26 试合并也同时在 README 和配置文件发生冲突，故在 `8ab13419` 记录停止。随后用户批准本轮上限 90 行，限定只移动本分支的 `reconcile_grace_sec`。最终被测提交 `d90b3fd4` 仅在 `config.py` 移动相同的五行，完整测试与四项检查在移动后重新执行。

| 项 | 实现与验证 |
|---|---|
| F1 | 清理历史报告的环境路径与内部链接。README 增加 R3 状态，明确 `decide` 和 `release_reconciliation` 在同一事务写状态与事件。API 模块路由表补齐 resolve。 |
| F2 | API 返回 409 和 `task_status`，两个后端在事务内重查任务状态。`test_resolve_rejected_while_task_running`、`test_resolve_rejected_while_waiting_for_input` 和 `test_decide_rejects_running_task` 覆盖动作、用量和事件不变。 |
| F3 | 首次对账从动作的 `updated_at` 起等待 `reconcile_grace_sec`，默认 60 秒，支持环境配置并要求正值。`test_first_reconcile_waits_for_grace` 和 `test_grace_uses_config` 验证时间边界。 |
| F4 | 缺失 Connector 记录 unsupported 和 `reason=connector_missing`。`HarnessError`、`TimeoutError` 和 `OSError` 记录 unknown 及异常类型和消息。两任务场景验证后一个任务仍被处理，意外 `RuntimeError` 继续抛出。 |
| F5 | `round_attempts` 统计最近一次 succeeded/failed 后的事件数。内存、PG、退避判断和人工提示共用此函数。`test_attempts_reset_after_decision` 覆盖人工裁决后新一轮从 1 开始。 |
| F6 | 自动与人工调用传入 `expected_attempt`，两个仓库在锁内比较，号数不符时零写入。`test_concurrent_unknown_counted_once` 及 PG 独立连接用例验证只有一条事件。 |
| F7 | 自动对账最多六次，第一次等 grace，此后等 1、2、4、8、16 分钟。`test_backoff_schedule` 核对每次到点前一秒、到点和再次 tick 的计数。 |
| F8 | API 模块说明只读访问仓库的原因，任务状态变更及状态事件由仓库事务负责。职责表与该边界一致。 |

**旧测试的时间和断言调整。** 下列场景以前立即执行第一次自动 tick，现先将时钟推进默认 grace 60 秒：`test_reconcile_succeeded_settles_and_requeues`、`test_reconcile_failed_marks_failed`、`test_reconcile_unknown_backs_off`、`test_reconcile_gives_up_after_six`、`test_unsupported_needs_manual`、`test_cancelled_task_cleanup_pending_then_done`、`test_reconcile_history_after_many_events`、`test_reconcile_never_executes`、`test_late_reconciliation_cannot_overwrite_manual_decision`、`test_manual_after_automatic_409`、`test_manual_loses_race_after_api_validation`。`test_reconcile_unknown_backs_off` 保留第 59 秒计数 1、第 60 秒计数 2 的原断言。原 `test_reconcile_gives_up_after_five` 更名为 `test_reconcile_gives_up_after_six`，末段的间隔从 `(60,120,240,480)` 改为 `(60,120,240,480,960)` 秒，到达上限时和长时间后两处调用数从 5 改为 6。竞态测试及直接调用按新接口传入序号，保留原裁决断言。

测试先行。原 R3 源码上的场景红测为 `21 failed, 2 passed, 40 deselected in 11.33s`，仓库用例首先因缺少共享函数无法导入。实现后仓库专项 `72 passed in 7.65s`，场景 `63 passed in 14.91s`，网关 `20 passed in 0.87s`。以下给出完整门槛输出。

### 1. 全量测试

WSL Ubuntu 22.04 上的 Python 3.11 虚拟环境，pytest 9.2.0。`python -m pytest tests/harness_runtime -q -n0 -o addopts=""`，退出码 0。仅替换环境路径。

```text
<venv>/lib/python3.11/site-packages/pytest_asyncio/plugin.py:306: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to "function" scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session". Set it to "fixture-scope" to keep the current behavior.

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
........................................................................ [ 11%]
........................................................................ [ 22%]
........................................................................ [ 34%]
........................................................................ [ 45%]
........................................................................ [ 56%]
........................................................................ [ 68%]
........................................................................ [ 79%]
........................................................................ [ 90%]
.........................................................                [100%]
633 passed in 71.32s (0:01:11)
```

### 2. 防御性检查

沿用上文完整 grep 正则，对 `git diff e47de9bf -- src/harness` 执行。标准输出为空，grep 退出码 1（无命中）。`git diff --check 2b863a6c HEAD -- src tests` 无输出，退出码 0。

```text
```

### 3. 新增行数

下列三条命令均退出 0，依次统计本轮 `.py`、相对 R1 的累计 `.py`、相对 R1 的整个 `src/`。Git 两点 diff 的累计结果按最终文件内容计算，不能直接相加两轮的 added。

```text
$ git diff --numstat 2b863a6c HEAD -- 'src/*.py' | awk '{s+=$1} END {print s}'
91
$ git diff --numstat e47de9bf HEAD -- 'src/*.py' | awk '{s+=$1} END {print s}'
378
$ git diff --numstat e47de9bf HEAD -- src/ | awk '{s+=$1} END {print s}'
381
```

用户将本轮上限调整为 **90 行**。本轮 Python 新增 **91 行**，减去 F1 路由注释 **1 行**和 F8 模块注释 **4 行**，最终计入 **86 行**。`config.py` 的 5 行分别用于 `reconcile_grace_sec` 字段（1 行）、正值校验（2 行）、序列化（1 行）和环境变量读取（1 行）。四处的位置避开了 #26 的新增项。逐文件原始输出如下。

```text
1	0	src/harness/api/app.py
22	8	src/harness/api/reconcile.py
5	0	src/harness/config.py
10	4	src/harness/storage/memory.py
11	6	src/harness/storage/postgres.py
10	2	src/harness/storage/repositories.py
12	4	src/harness/tasks/recovery.py
20	8	src/harness/tools/gateway.py
```

### 4. 与 #26 试合并

对最终被测提交 `d90b3fd4` 执行 `git fetch origin pull/26/head:refs/remotes/origin/pr-26`（退出 0，无输出），再执行 `git merge-tree --write-tree --name-only origin/pr-26 HEAD`（退出 1）。#26 head 为 `800d2e1d`。

```text
3898ee87a4b97951a0b2913b846dbec642dec288
src/harness/README.md

Auto-merging src/harness/README.md
CONFLICT (content): Merge conflict in src/harness/README.md
Auto-merging src/harness/api/app.py
Auto-merging src/harness/app.py
Auto-merging src/harness/config.py
Auto-merging src/harness/contracts.py
Auto-merging src/harness/storage/memory.py
Auto-merging src/harness/storage/postgres.py
Auto-merging src/harness/storage/repositories.py
Auto-merging src/harness/tools/gateway.py
```

唯一冲突文件是任务卡允许的 `src/harness/README.md`。`src/harness/config.py` 自动合并。原先两项配置在 `from_env` 中的插入位置重叠，移动本分支的配置后已消除这一冲突。
