# C5 观测包、决策校验、待看区与经过回写

**验收结果**　全量回归 1020 项通过，BASE 为 912 项通过。本卡增加 108 个 pytest 用例，覆盖内存和 PostgreSQL。防御性写法检查输出为空。按卡片口径，`src/` 新增 574 行，上限 860 行。

本卡基于 2026 年 11 月 26 日 fetch 后核对的 `origin/DaraOS` 提交 `9aadb65c190172293c466bbe0723d6d9d70cfd79`，本地分支为 `feat/harness-p2-watch`。运行环境为 WSL 2 Ubuntu 22.04、Python 3.11.15 和 Docker PostgreSQL 16。代码与测试通过同一 Git tree 在 WSL 的 Linux 文件系统上检出并执行。最终用于检查的 tree 是 `63db46f326d466a755ffcbb602dc977ab613dfec`，包含全部新增测试和生产文件，随后才加入本报告。

## 交付内容

| 环节 | 实现及验证 |
|---|---|
| Worker 组装观测 | 两种 watch 都得到单独的 user 观测。偏好按置顶、关联词命中数和更新时间排序。稳定偏好前 10 条带截断正文，后 20 条仅列引用和摘要。经过按最近三个 UTC 自然日分组，最近提醒按订阅过滤，事实只给目录。日历按准入顺序取事件，分析复核加入 C3 基线、触发版本、复算结果和阈值。 |
| 引用与恢复 | `memory_refs` 来自观测包的结构化引用字段。用户正文里即使出现外形正确的记忆 ID，也无法增加证据资格。恢复 checkpoint 时，专用 builder 调用现有恢复器，再从实际重放的观测恢复引用，避免把两次尝试之间新增的偏好授权给旧消息。其他能力继续使用原有恢复流程。 |
| 决策与复算 | 两份英文能力 Prompt 各包含一份提醒和一份静默的完整 JSON 示例。三个 checker 各自读取本任务的动作。证据校验核对当前 Attempt 所存 `context.memory_refs`、成功 `memory.read` 的结果、scope 和记忆版本，并拒绝 `EPISODE`。分析校验按 C3 的 `recompute_recorded` 事件定位产物，核对列名和 0.005 容差，验证文件摘要并用 `open_readonly` / `run_readonly` 重跑，最后关闭连接。 |
| 交付与 Main 读取 | 普通提醒分数为 7，复算失败为 8，静默决策为 0。Harness 从准入记录计算去重键。免打扰、订阅状态和路径授权影响交付。日历根据已验收的事件引用比较同一日历条目的版本。数据复核先查最新外部事件，遇到其他类型才分页查同一数据源的最近 `source_changed`。判断新鲜度只查库存事件和验收记录。Main 得到相关项的指针，主动读取后才得到决策正文。静默和已取消项均不注入。 |
| 经过与候选 | 成功提交后写一条 `EPISODE`，按附件以 `task.created_at + 3 天` 为到期时刻，保存决策、理由和证据引用。每条候选使用 `task:{task.id}:proposal:{idx}`。测试以同一任务写入两条不同的偏好并重放，数据库里仍是原来两条。已有偏好的同内容提议被过滤。提交时撤销获胜的任务不会写经过。 |

生产修改涉及 app 接线、上下文、交付服务、三个新模块和两份 Prompt。通过 AST 检查，BASE 上的 11 处 `SimpleContextBuilder` 构造和 1 处 `DeliveryService` 构造都显式传入 `memory`。C3 的分析测试套件使用真实的 `self.memory`，无需记忆能力的旧测试与 smoke 脚本使用 `NoMemory`。原有测试只补构造依赖。

## 测试先行与复现

基线运行结果为 `912 passed in 111.02s (0:01:51)`，失败用例列表为空。完整命令和输出见[基线记录](p2-C5-baseline.txt)。

- 先在 BASE 上加入全部 19 个具名必测场景与 Prompt 示例检查，源文件差异为空。第一批产生 `98 failed in 49.08s`。补充的 SQL 实际重放与提交时撤销测试在两个后端得到 `4 failed in 7.72s`。BASE 上的 user 消息没有观测包，两份 Prompt 也仍是占位符。
- 独立复现了 checkpoint 恢复时旧消息与新引用不一致，内存和 PostgreSQL 各失败一次。修复后又覆盖了旧格式 checkpoint。三个 checker 的动作仓库读取也先单独跑红（`2 failed in 8.06s`），修复后决策校验定向套件 `68 passed in 48.61s`。
- 交付定向回归为 `26 passed in 25.80s`。观测与原 C3/P1 兼容用例为 `25 passed in 11.35s`。这些用例都包含在下方全量回归里。
- smoke 脚本另用 `ScriptedModelAdapter` 运行一次，经过两次工具调用和三次模型步骤，最终提交状态为 `succeeded`。这次 smoke 验证了共用 builder 的旧脚本调用路径。两种巡检本身使用脚本模型做端到端验证，真实模型效果和轨迹导出交由 C6 验收。

## 任务卡校准与已知问题

- 原 C5 写明的构造点数量已被附件校准。分析新鲜度按附件先调用 `latest_event`，再按需分页。C5 任务卡列出的分析触发字段为 `ref/path/old_sha256/new_sha256`。记忆计划 §5.3 另列文件大小，实际观测按卡采用前四项。
- 一页设计稿和记忆计划概述了每条复算查询的差额与相对变化。C3 实际在每条查询保存旧值、新值和行数，在 `recompute.threshold` 保存阈值查询的差额与相对变化。观测包沿用 C3 的实际格式。C3 复算出错时产物里的 `new_sha256` 为空，待看项和去重键取触发时准入快照的 `new_sha256`，因此失败提醒仍能按正确版本显示。
- 在两类 watch 的 user 消息中明确给出任务创建时刻与 TTL 终点，供模型生成严格落在开区间内的 `valid_until`。观测包的既定 JSON 结构不变。两份 Prompt 均要求把证据留在决策产物，提交结果的 evidence 留空。
- 决策校验把实际核实的引用保存在 `decision_evidence.evidence_refs`。日历待看项直接使用这份数据库验收记录做新鲜度检查，读收件箱时无需重开决策产物。订阅已失效时，`prepare` 可计算出静默结果。两种存储原有的发布事务会忽略已失效订阅。正常静默决策会留存并从收件箱隐藏。
- 独立诊断复现了附件点名的 C3 基线失效路径。调用 `MemoryService.forget` 撤回订阅关联的基线，再提交一次 `source_changed`，C3 因异常处理分支缺少 `NotFound` 导入而中断，这次数据变更未能触发新的巡检任务。该文件的 Git blob 与 BASE 完全一致。C5 按指令将问题记录在此。

```text
ValueError: baseline_ref must name a current ANALYSIS memory
tasks/proactive_controller.py:229 | except (ValueError, NotFound):
NameError: name 'NotFound' is not defined
```

复现日志保存在 WSL 的 `/home/xue/c5-watch.AAC9zA/`。下面是卡片指定的三条命令及其实际完整输出。

## 提交前的三项检查

### 1. Harness 全量回归

```bash
python -m pytest tests/harness_runtime -q -n0 -o addopts="" -m "integration or not integration"
```

```text
........................................................................ [  7%]
........................................................................ [ 14%]
........................................................................ [ 21%]
........................................................................ [ 28%]
........................................................................ [ 35%]
........................................................................ [ 42%]
........................................................................ [ 49%]
........................................................................ [ 56%]
........................................................................ [ 63%]
........................................................................ [ 70%]
........................................................................ [ 77%]
........................................................................ [ 84%]
........................................................................ [ 91%]
........................................................................ [ 98%]
............                                                             [100%]
1020 passed in 185.08s (0:03:05)
```

进程退出码为 0。基线和最终回归的失败用例列表均为空。

### 2. 防御性写法检查

```bash
git diff 9aadb65c190172293c466bbe0723d6d9d70cfd79 -- 'src/harness/*.py' ':!src/harness/contracts.py' ':!src/harness/interfaces.py' ':!src/harness/storage/repositories.py' \
  | grep -nE '^\+.*(except Exception|except:|getattr\([^)]*,[^)]*,|hasattr\(|self\._\w+ is not None)|^\+\s+def __init__\([^)]*=None|^\+ {8,}(from \.|import )'
```

```text
```

原始输出为 0 字节。管道中 `git diff` 退出码为 0，`grep` 退出码为 1，表示没有匹配行。

### 3. 生产代码新增行数

```bash
git diff --numstat 9aadb65c190172293c466bbe0723d6d9d70cfd79 -- src/ ':!src/harness/prompts' ':!src/harness/capabilities' | awk '{s+=$1} END {print s}'
```

```text
574
```

统计包含已加入索引的 `observation.py`、`decision.py` 和 `watch_delivery.py`。与 860 行上限相差 286 行。

## 复核后补充微调与真模型预验

在开 PR 前针对复核意见做了 4 处非阻塞微调，并完成真实模型 M1 / D2 预验：

1. `ProactiveDecision.draft` 与 `valid_until` 补充默认值 `None`（仍由 `delivery_fields` 强制 `propose_notify` 非空、`silent` 为空），避免真实模型在 `silent` 决策省略空字段时误挂 schema；补充 `test_silent_decision_may_omit_null_delivery_fields`。
2. `MemoryProposal.summary` 与 `text` 补充 `min_length=1`，阻止空白候选写入记忆库；补充两条参数化测试。
3. `WatchChecks._replay` 改为按准入记录 `input_snapshot["path"]`（`.expanduser().resolve()`）校验文件 `sha256`，与 C3 `Recomputer._run` 的多数据源定位口径一致。
4. `src/harness/prompts/analysis_watch.md` 补充一行示例说明：当同一期间订单数不变而销售额变化时，应判定为对已计入记录的修订而非新增订单。

复跑结果（WSL 2 + Docker PostgreSQL 16）：
- 集成测试（`-m integration`）：`276 passed`；非集成测试（`-m "not integration"`）：`750 passed`；合计 **`1026 passed, 0 failed`**（较基线 +114 用例）。
- 防御性写法检查：**0 处命中**；生产代码新增行数：**577 行**（上限 860 行）。
- 真实模型预验（`deepseek-chat`，`temperature=0`）：
  - **M1（日历提醒）**：一次通过（`succeeded`，4/4 checks passed，入箱 1 条，引用日历事件与已确认偏好，写明组会由周五改至周四）。
  - **D2（3 月补价复核）**：一次通过（`succeeded`，5/5 checks passed 含 DuckDB 现场重跑 `recompute_replay`，入箱 1 条，准确输出 `219912.81 -> 232944.31 / +5.93%` 与订单数 `169 -> 169` 不变，判定为对已计入记录的修订）。
