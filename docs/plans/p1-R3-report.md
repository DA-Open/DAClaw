# P1 R3 实施与行数上限停止报告

日期：2026-11-18。状态：**功能测试通过；累计新增源码 316 行，超过 300 行上限，按任务卡停止，R3 尚未交付完成。**

第一次执行（11-17）因缺少待对账任务发现接口停止；11-18 的任务卡第 5 条补充 `TaskRepository.list_reconciliation_candidates()` 后继续。第二次执行复现人工 succeeded 被迟到自动 failed 覆盖；11-18 第 6 条和用户明确授权新增原子 `ActionRepository.decide`，并将上限改为 300。本次解决并发问题，保留原回归断言，但触发行数上限停止条件。

## 分支、提交与范围

- Worktree：`E:\Desktop\研0\DAClaw-worktrees\harness-p1-reconcile`。
- 分支：`feat/harness-p1-reconcile`，沿用原 worktree，没有新建分支或 rebase。
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

原始文件位于 `E:\Desktop\研0\outputs\p1-20261118\`：`R3-atomic-storage-red.txt`、`R3-atomic-gateway-red.txt`、`R3-atomic-storage-focused.txt`、`R3-atomic-pytest-full.txt`。

新验证覆盖：既有 `test_late_reconciliation_cannot_overwrite_manual_decision`（断言未改，内存和 PG）；`test_manual_after_automatic_409`；PG 两个独立连接同时裁决只产生一次结算和一条对账事件；人工请求通过检查后输掉竞态；全部四种迟到自动结果无写入；终态 no-op、作用域及动作归属、原调用和 retry 预留隔离、空任务释放。取消测试按更新后的任务卡改为调用 decide，保留任务仍为 cancelled 的断言，并检查原事件不变、仅新增裁决事件。

只读独立审查未发现新的原子裁决正确性缺陷；这不替代下方测试证据，也不豁免行数上限。

## 当前停止原因

任务卡通用约定：`src/ 下新增行数不超过本卡上限（不含 tests/）。超出时停下来报告，不要压缩代码来凑数。`

相对 BASE 的新增源码为 **316 行**，上限 **300 行**，超出 **16 行**。发现超限后停止所有代码修改，未压缩格式、删除测试、放宽断言或擅自提高上限。随后仅运行检查、只读审查和保存本地代码及报告。

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

恢复工作需要明确调整该行数约束或给出可删除的功能范围；当前代码不能标为满足任务卡。

## 四项检查原始输出

环境固定为 WSL Ubuntu-22.04 的 `/home/xue/venvs/harness-pg/bin/python`，未使用挂载盘 DAClaw/.venv。PG 测试按已有 fixture 自建临时库。pytest-asyncio 的既有弃用提示原样保留，没有修改配置。

### 1. 完整测试

等价于在该 worktree 中用上述解释器执行：

```text
python -m pytest tests/harness_runtime -q -n0 -o addopts=""
```

原始输出（退出码 0）：

```text
/home/xue/venvs/harness-pg/lib/python3.11/site-packages/pytest_asyncio/plugin.py:306: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
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

grep 退出码 1，表示没有匹配项。Windows 实际使用 `C:\Program Files\Git\usr\bin\grep.exe`，正则和输入不变。`git diff --check 0fd199be -- src/harness tests/harness_runtime` 也没有空白错误。

### 3. 源码新增行数

```text
git diff --numstat 0fd199be -- src/ | awk '{s+=$1} END {print s}'
```

原始输出（awk 退出码 0；业务门槛失败）：

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

## 飞书与后续交付

R3 的行数门槛尚未满足，因此没有向飞书开发记录写入 P1 表格，也没有写入后的读回结果。现有内容、6 张原生表格和 3 个嵌入表格未修改。此前只读快照为 revision 137、155 个顶层块，不能当作写入读回证据。

R1 仍为 `feat/harness-p1-resume` / `0fd199be`，原完整测试为 478 passed in 46.00s；其报告保留四项检查原始输出。R3 当前 583 项测试通过，仍需满足源码行数约束后才能建飞书 P1 表格、读回并更新本报告。后续一起 rebase 到 DaraOS、push 和开 PR 的阶段尚未启动；本次均为本地提交。
