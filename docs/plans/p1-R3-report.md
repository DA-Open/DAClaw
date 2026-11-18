# P1 R3 完成报告

日期：2026-11-18。状态：**R3 实现与四项检查完成；完整测试 583 项通过，源码新增 316 行，符合用户最新授权。**

第一次执行（11-17）因缺少待对账任务发现接口停止；11-18 的任务卡第 5 条补充 `TaskRepository.list_reconciliation_candidates()` 后继续。随后复现人工 succeeded 被迟到自动 failed 覆盖；第 6 条授权原子 `ActionRepository.decide` 后修复，原回归断言未改。因内存和 PG 两份 decide 实现使累计新增源码达到 316 行，曾按 300 行上限停止；11-18 用户明确将上限调整为 316，并要求源码不再修改。本次仅完成报告和交付收尾。

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

## 飞书写入与读回

已在[飞书开发记录](https://my.feishu.cn/wiki/FpxWwLLfMiD1ytkmRbUcUwx8nbc)文末追加“P1 可靠执行（2026-11-18）”标题、说明和一张 6 列原生表格。六行分别为 R1、R3、R2、S1、P1 验收、R4，后四行均标为“未开始，等待 …”。R1、R3 的提交号为 rebase 前的实际验证版本；表前说明了后续发布会产生新提交号。

写入使用 `docs +update --command append --revision-id 137`，服务返回 success、revision 138、warnings 空。随后用 `docs +fetch --detail full --doc-format xml` 读回；原 155 个顶层块按原顺序逐块 OuterXml 比对全部相同，原 6 张原生表格和 3 个嵌入表格没有修改，仅增加 3 个顶层块及 1 张原生表格。新表每行 6 列，表头和六个任务标签逐项核对。

[新表直达链接](https://my.feishu.cn/wiki/FpxWwLLfMiD1ytkmRbUcUwx8nbc#doxcndkYe7ehNa70h46mBj5zaXg)。写入前、写入响应、读回和比较输出分别保存在 `E:\Desktop\研0\outputs\p1-20261118\feishu-p1-before.json`、`feishu-p1-append.json`、`feishu-p1-after.json`、`feishu-p1-readback.txt`。原文全文快照仅保存在本地输出目录，没有加入 Git 提交。

读回比较原始输出：

```text
before_revision=137
after_revision=138
original_top_level_blocks=155
unchanged_original_top_level_blocks=155
new_top_level_blocks=3
native_tables=6->7
embedded_sheets=3->3
new_table_id=doxcndkYe7ehNa70h46mBj5zaXg
table_rows=7
PR | 状态 | 分支 | 提交号 | 测试结果 | 依赖
R1 | 已完成，本地验证通过 | feat/harness-p1-resume | 0fd199be267f6de894d0a45d39d081c7a195c7fb | 478 passed in 46.00s；防御性检查无命中；src 新增 210/350 行；#26 试合并无冲突。四项原始输出见 docs/plans/p1-R1-report.md。 | 基于 #22 head 2929550e；#22 已合入 DaraOS。后续与 R3 一起 rebase 后开 PR。
R3 | 已完成，本地验证通过 | feat/harness-p1-reconcile | af78765541636cd0f77f9671c26c7f293a005350 | 583 passed in 60.59s；防御性检查无命中；src 新增 316/316 行；#26 试合并无冲突。含内存/PG 并发裁决回归；原覆盖竞态断言未改。四项原始输出见 docs/plans/p1-R3-report.md。 | 基于 R1 最终提交 0fd199be。原子裁决接口及 316 行上限已获授权；不依赖 R2，无真实飞书调用。
R2 | 未开始，等待 R1、#26 合入及 #23 去留确定 | feat/harness-p1-approvals（计划） | — | 未运行 | DaraOS 必须已有 Approval 类型和 0003 迁移；#23 去留已定。本次不实施。
S1 | 未开始，等待 R1 和 #26 合入 | chore/harness-unify-reclaim（计划） | — | 未运行 | 接通 Scheduler 与 Recovery；保留 #26 原测试断言。本次不实施。
P1 验收 | 未开始，等待 R1、R2、R3、S1 全部合入 | test/harness-p1-acceptance（计划） | — | 未运行 | 在 DaraOS 上验证真实 API/Worker/PG 五个场景；只加测试和文档。本次不实施。
R4 | 未开始，等待 P1 验收合入及真实飞书测试凭据 | feat/harness-feishu-send（计划） | — | 未运行 | 先实测同一 uuid 连续发送两次的响应，再按证据实现 Connector。本次不实施。
```

## 后续发布阶段

R1 仍为 `feat/harness-p1-resume` / `0fd199be`，原完整测试为 478 passed in 46.00s，其报告保留四项检查原始输出。R3 本次 583 项测试通过，316 行上限和保留 release_reconciliation 已获用户明确授权，飞书追加及读回已完成。

截至本报告收尾提交，均为本地提交，没有 push 或开 PR。接下来按用户“R1 和 R3 都做完以后，再一起 rebase 到 DaraOS 上开 PR”的明确指令进入发布阶段；R2、S1、P1 验收、R4 不启动。
