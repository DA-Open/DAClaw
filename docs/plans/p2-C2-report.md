# P2 C2 模拟日历与时间窗巡检执行报告

2026-11-24 · 分支 `feat/harness-p2-personal-window`

BASE：`5d765337222ebc2bc87d8b50123a0b6bd07eea9a`（C0 / PR #32）。按本次派发要求直接使用这个提交，不等待 C0 合入，也不自行 rebase。已使用 `attach_worktree` 挂载 `E:\Desktop\研0\DAClaw-worktrees\harness-p2-personal-window`；开始时分支、HEAD 均符合要求，工作区干净。代码、测试、文档和本地提交均在这个 worktree 中完成。

## 完成内容

- 新增 `CalendarChange`、`CalendarFile`、`PersonalWindowEvent`。日历字段按卡片限制长度、版本、变更类型和带时区的时间，额外字段被拒绝。窗口必须满足 `since < until`。
- `MockCalendarSource.changes` 读取本地 JSON，返回 `(since, until]` 内的变化。文件缺失、JSON 错误或字段校验失败统一抛出带路径的 `ValueError`。
- 订阅入口支持 `personal_watch@1`：必须有合法 cron，不能带事件过滤器、基线或 watch；资源只能是 `subject_ref` 指向的 JSON 文件，同时要求 Proactive 和 Main 路径授权。HTTP 测试核实缺少任一角色授权时返回 403，并在错误中写明角色。
- Scheduler 首次从订阅创建时间开始，后续从已记录窗口的最大 `until` 接续，漏跑的整点不会形成空档。原始变化使用 `item_id@version` 去重，append 后立即标为已处理，再发送窗口事件。权限撤销时不读文件；来源错误只跳过该订阅并记录日志。
- 内存和 PostgreSQL 仓库实现同一 `changes` 接口：按 scope、对象、外部事件类型、来源及 `(since, until]` 过滤，按 `(occurred_at, seq)` 排序。
- `decide_personal` 按任务卡顺序处理停用、撤权、空窗口、订阅忙碌、日预算、scope 容量和准入。快照使用系统生成的 `event:<Event.id>` 引用及 `personal-admission@1`。延后窗口继续由已有 `pending_events` 重评，保留原先的评估记录。
- 新任务使用后台通道、`schedule` 触发、`decision` 产物和指定的三个检查项。能力包按卡片配置，Prompt 只有一行 `TODO(C5)`；`personal/__init__.py` 只有一行 docstring。

没有改动 BASE 中的任何测试文件。原有数据卡 `decide` 函数、`_request` 返回内容、`_origin`、数据事件校验和 evaluate 的任务创建逻辑经 AST 比对保持一致；`on_event` 只在这些原有逻辑外围增加能力分派。`/events` 没有改动。改动文件均在 C2 允许范围及通用约定要求的两个报告文件内。

## 环境与红绿测试记录

直接在上述 worktree 的 WSL 挂载路径运行，没有另建代码副本：

- WSL 2：`Ubuntu-22.04`。
- Python：`3.11.15`，运行前激活 `/home/xue/venvs/docker-harness-review`。
- Docker：`29.6.1`；复用本地缓存的 `postgres:16`，由既有 fixture 各次自建并清理临时数据库。
- 本卡独立临时目录及日志：`/home/xue/p2-c2-tests.G2iTKz`。运行时设置 `TMPDIR` 指向该目录，未修改共享虚拟环境。
- Git 的 WSL 只读检查显式设置该 worktree 对应的 `GIT_DIR` 和 `GIT_WORK_TREE`，因为 `.git` 指针使用 Windows 路径。检查进程使用 `core.autocrlf=true`、`core.safecrlf=false`，按仓库的文本规范化规则生成 diff，避免仅由 LF/CRLF 提示污染输出；未写入仓库或全局 Git 配置。

先在 BASE 运行第一项全量命令，再新增测试。[基线失败清单](p2-C2-baseline.txt)为 0 条，完整输出如下：

```text
........................................................................ [  9%]
........................................................................ [ 19%]
........................................................................ [ 29%]
........................................................................ [ 38%]
........................................................................ [ 48%]
........................................................................ [ 58%]
........................................................................ [ 68%]
........................................................................ [ 77%]
........................................................................ [ 87%]
........................................................................ [ 97%]
....................                                                     [100%]
740 passed in 141.64s (0:02:21)
```

随后先写必需测试，源代码仍在 BASE 时分别运行两个新测试模块：

```bash
python -m pytest tests/harness_runtime/unit/test_calendar_source.py -q -n0 -o addopts=""
python -m pytest tests/harness_runtime/scenarios/test_personal_window.py -q -n0 -o addopts="" -m "integration or not integration"
```

红灯摘要及原因：

```text
ModuleNotFoundError: No module named 'harness.personal'
1 error in 0.62s
unit_exit_code=2

30 failed in 10.23s
scenario_exit_code=1
```

场景失败来自尚未支持 `personal_watch@1`，以及内存和 PG 仓库尚无 `changes`。原始输出保存为独立临时目录里的 `red-unit.log`、`red-scenarios.log`。

实现后的第一次定向检查为 `2 failed, 53 passed in 13.63s`。两处失败均是本次新增测试将能力包的 `tools: frozenset[str]` 与列表比较；核对 `capabilities/registry.py` 后，将新增断言改成精确的 frozenset 相等判断，预期工具集合不变。没有修改或放宽 BASE 已有断言。还将容量优先级测试改为实际占用一个后台任务名额。

最终定向检查：

```bash
python -m pytest tests/harness_runtime/unit/test_calendar_source.py tests/harness_runtime/scenarios/test_personal_window.py -q -n0 -o addopts="" -m "integration or not integration"
```

```text
.......................................................                  [100%]
55 passed in 10.62s
```

九项必需测试全部存在：

| 用例 | 验证内容 |
|---|---|
| `test_mock_calendar_window_bounds` | 严格左开右闭；配套参数化用例覆盖时间、长度、JSON 和缺失文件错误 |
| `test_personal_subscription_validation` | cron、过滤器、资源、基线、watch 与双角色授权；`/events` 不接受伪造的 personal window |
| `test_first_tick_admits_without_prior_event` | 首次 02:00:05 巡检建立 `(00:00, 02:00]` 窗口，只有一条已处理原始事件、一条 scheduler 事件和一个后台任务 |
| `test_raw_calendar_event_not_readmitted` | 重建系统后在相同时刻再次 tick，原始事件不进入 pending，也不新增准入 |
| `test_empty_window_skips_without_task` | `skip/no_changes`，不建立任务 |
| `test_missed_ticks_leave_no_gap` | 漏掉两个整点，下一窗口从前一 `until` 接续；空窗口优先于忙碌判定 |
| `test_reingest_dedup_and_new_version` | 同版本重复只入库一次；下一窗口仅引用 v3 |
| `test_revoked_path_does_not_read_source` | 读取即报错的替身没有被调用，记录 `skip/resource_permission_revoked` |
| `test_busy_subscription_defers_then_runs` | 忙碌时 defer，先前任务结束后由 pending 重评并 admit，保留旧评估 |

所有场景和仓库用例复用 `test_repositories.py` 的 `repos` fixture，在内存与 PostgreSQL 上执行同一套断言；时间使用 `fakes.Clock`。补充用例覆盖来源错误后继续处理其他订阅、首次到期前跳过、准入优先级、忽略非窗口历史记录，以及仓库的过滤、排序和读回隔离。

## 提交前的三项检查

### 1. Harness 全量回归

```bash
python -m pytest tests/harness_runtime -q -n0 -o addopts="" -m "integration or not integration"
```

完整输出（退出码 0）：

```text
........................................................................ [  9%]
........................................................................ [ 18%]
........................................................................ [ 27%]
........................................................................ [ 36%]
........................................................................ [ 45%]
........................................................................ [ 54%]
........................................................................ [ 63%]
........................................................................ [ 72%]
........................................................................ [ 81%]
........................................................................ [ 90%]
........................................................................ [ 99%]
...                                                                      [100%]
795 passed in 125.84s (0:02:05)
```

相对 BASE 的 740 项新增 55 项，没有失败或跳过，失败清单仍为空。原始输出为 `final-full.log`。

### 2. 防御性写法检查

```bash
git diff 5d765337222ebc2bc87d8b50123a0b6bd07eea9a -- 'src/harness/*.py' ':!src/harness/contracts.py' ':!src/harness/interfaces.py' ':!src/harness/storage/repositories.py' \
  | grep -nE '^\+.*(except Exception|except:|getattr\([^)]*,[^)]*,|hasattr\(|self\._\w+ is not None)|^\+\s+def __init__\([^)]*=None|^\+ {8,}(from \.|import )'
```

完整输出为空：

```text
```

捕获 stdout 与 stderr 的 `defensive-full.txt` 为 0 字节。grep 退出码 1 表示无匹配。新增 import 均在模块顶部；异常处理仅为日历读取的 `FileNotFoundError` / `ValidationError`，以及调度器按卡片要求捕获的 `ValueError`。`previous=None` 是 Pydantic 可选字段，属于通用约定的明确豁免；Scheduler 的 `calendar` 构造参数没有默认值。

### 3. `src/` 新增行数

新增文件已纳入 Git 索引，统计包含全部新源码。

```bash
git diff --numstat 5d765337222ebc2bc87d8b50123a0b6bd07eea9a -- src/ ':!src/harness/prompts' ':!src/harness/capabilities' | awk '{s+=$1} END {print s}'
```

完整输出：

```text
171
```

未超过 C2 的 360 行上限。另执行 `git diff --check`，无输出。

## 文档差异、衔接与边界

- 任务卡的通用 BASE 写作“C0 合入后的 DaraOS”；本次派发明确要求从 PR #32 的指定提交开发并由派发方负责合入后 rebase，因此本次以派发 BASE 为准。
- 《P2 Proactive Agent 一页设计稿》§8 的订阅入口概述写“按 capability_ref 选择事件校验”。C2 明确要求 `/events` 不变、`on_event` 按 payload 的 `kind` 选模型、按订阅能力选准入函数。本次执行 C2：公开 API 仅放开创建 personal 订阅，窗口事件由 Scheduler 生成。
- 《记忆系统计划》§5.2 的观测包含日历事件内容；本卡只落地原始事件及准入快照里的事件引用，观测包正文交给 C5。引用采用系统生成的 `Event.id`，条目 ID 和版本保留在 payload。
- `memory.search`、`memory.read` 由 C1 提供；`decision_schema`、`decision_evidence` 与 Prompt 正文由 C5 提供。C2 不运行 personal 任务的模型及验收，不声称已经完成提醒交付。当前若运行到未注册的两个检查项，会按卡片说明得到 `inconclusive`。
- 去重键严格沿用卡片的 `(scope_id, "mock_calendar", item_id@version)`；`calendar_id` 不参与去重，同一 scope 的多个日历来源需避免复用同一条目 ID 和版本。没有改动唯一索引或增加迁移。

没有触发 C2 停止条件，没有超范围修改或遗留的 C2 必需项。本次只在本地提交，不 push、不开 PR；C0 合入后的 rebase，以及并行分支的 `personal/__init__.py` / 分派位置冲突，由派发方按任务卡处理。
