# P2 C4 停止报告

2026-11-24 · 本地分支 `feat/harness-p2-delivery`

**C4 未实施：开工检查已触发卡片的停止条件。** 演示脚本存在卡片未列出的 `DeliveryRepository.ack` 调用方。只交付本报告和基线记录，不修改实现、测试、能力包或 Prompt，不 push、不创建 PR。

## 工作树与基线

- 已通过 `attach_worktree` 挂载 `E:\Desktop\研0\DAClaw-worktrees\harness-p2-delivery`。
- 开工时分支为 `feat/harness-p2-delivery`，HEAD 为指定 BASE `5d765337222ebc2bc87d8b50123a0b6bd07eea9a`，`git status --short` 输出为空。
- 已读取任务卡“0. 顺序与依赖”“通用约定”和 C4，以及仓库 AGENTS.md、Harness README 要求的四节。
- 在指定 worktree 用 `git archive` 导出 BASE，测试副本为 WSL 的 `/home/xue/harness-p2-C4-check.3hM6pD/repo`。使用 `/home/xue/venvs/docker-harness-review/bin/python`（Python 3.11.15）；实际导入的 `harness` 路径已确认位于这个测试副本。
- PostgreSQL fixture 使用已有 `postgres:16` 镜像启动独立临时容器；没有复用其他任务的测试数据库。
- [基线失败清单](p2-C4-baseline.txt)为空，740 个用例通过。由于未修改 `src/`、`tests/` 或 `scripts/`，本次基线执行也作为提交前回归证据，没有重复运行相同测试。

## 停止条件与证据

任务卡 `E:\Desktop\研0\P2_Codex任务卡_2026-11-24.md:684` 明确写明：

> `DeliveryRepository.ack` 还有本卡没列出的调用方。

在 BASE 执行 `rg -n '\.ack\(' src tests scripts` 的完整输出为：

```text
scripts\harness\proactive_data_card_demo.py:79:    await system.repos.delivery.ack("demo", items[0].id, "handled", "useful", system.delivery.clock())
src\harness\api\proactive.py:90:        return await system.repos.delivery.ack(request.state.actor.scope_id, item_id, payload.status, label, system.delivery.clock())
tests\harness_runtime\scenarios\test_proactive.py:189:    await repos.delivery.ack("s1", item.id, "handled", "useful", utcnow())
```

API 和场景测试已列入 C4；`scripts/harness/proactive_data_card_demo.py` 没有列入。现有接口在 `src/harness/storage/repositories.py:339` 接收五个参数（不含 `self`），而 C4 要增加必传的 `label_source: str | None`。只更新卡片列出的调用方，会遗漏演示脚本中的现有五参数调用。为新参数添加默认值来迁就旧调用也不符合通用约定。

发现此条件后没有编写“必须的测试”或实现，也没有修改 `test_verifier.py` 的 Main / Proactive 白名单。红灯阶段尚未开始，不声称 C4 行为已经得到验证。

## 提交前三项检查

### 1. Harness 全量回归

在 WSL 2 Ubuntu-22.04 中激活指定环境、进入 BASE 测试副本，并清除 `DARAOS_HARNESS_TEST_DSN` 后执行：

```bash
python -m pytest tests/harness_runtime -q -n0 -o addopts="" -m "integration or not integration"
```

完整 pytest 输出：

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
740 passed in 89.76s (0:01:29)
```

pytest 返回码为 0。原始输出保留在 WSL 的 `/home/xue/harness-p2-C4-check.3hM6pD/baseline-full.txt`。包装脚本在 pytest 完成后，末行受 PowerShell 的 CRLF 影响，额外报出 `bash: line 15: exit: 0\r: numeric argument required`，使外层命令返回 1；此前从 `PIPESTATUS[0]` 保存的 `baseline-exit.txt` 字节为 `30 0A`（`0\n`），因此该包装错误不计为测试失败。

### 2. 防御性写法检查

在交付 worktree 中使用 Git Bash 执行任务卡原命令：

```bash
git diff 5d765337222ebc2bc87d8b50123a0b6bd07eea9a -- 'src/harness/*.py' ':!src/harness/contracts.py' ':!src/harness/interfaces.py' ':!src/harness/storage/repositories.py' \
  | grep -nE '^\+.*(except Exception|except:|getattr\([^)]*,[^)]*,|hasattr\(|self\._\w+ is not None)|^\+\s+def __init__\([^)]*=None|^\+ {8,}(from \.|import )'
```

完整输出为空（0 字节）；`grep` 返回 1 表示没有匹配。

```text
```

### 3. `src/` 新增行数

在交付 worktree 中使用 Git Bash 执行任务卡原命令：

```bash
git diff --numstat 5d765337222ebc2bc87d8b50123a0b6bd07eea9a -- src/ ':!src/harness/prompts' ':!src/harness/capabilities' | awk '{s+=$1} END {print s}'
```

完整输出仅含一个换行（1 字节，十六进制 `0A`）：

```text

```

这里 `git diff --numstat` 没有输入行，原命令中的 `awk` 变量 `s` 保持未初始化，故打印空行。实际 `src/` 新增 0 行，低于 C4 的 200 行上限。补充执行以下命令，输出为空、返回 0，确认源码、测试及脚本均与 BASE 相同：

```bash
git diff --exit-code 5d765337222ebc2bc87d8b50123a0b6bd07eea9a -- src tests scripts
```

## 差异、未完成工作与恢复条件

- 实际仓库比任务卡多出一个 `ack` 调用方，这是本次需要修订的范围差异。没有为绕过限制修改脚本、添加兼容默认值或调整接口设计。
- 任务卡写 BASE 为“C0 合入后的 DaraOS”；本次派发明确覆盖为 PR #32 的提交 `5d765337222ebc2bc87d8b50123a0b6bd07eea9a`，且 rebase 由派发方负责，因此按指定提交验证。
- 本次没有使用记忆系统计划推导额外要求，也没有进入功能实现；读取 / 提起拆分、新能力包、标签来源、静默评价和 Prompt 调整均未完成。
- 建议将 `scripts/harness/proactive_data_card_demo.py` 的该处 `ack` 调用补入 C4 的“允许改”，允许显式补齐与演示反馈一致的 `label_source`。范围修订后可从本分支继续，先添加卡片要求的测试并确认红灯，再实施功能。
- C4 列出的共享接口变化仍需在后续 PR 描述中单独说明；`document_draft@2` 的旧 Prompt 与新读取语义之间的差异也仍需按卡片记录。
