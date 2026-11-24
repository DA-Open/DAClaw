# P2 C4 交付报告

2026-11-24 · `feat/harness-p2-delivery` · BASE `5d765337222ebc2bc87d8b50123a0b6bd07eea9a`

Main 可以反复读取后台结果。决定把结果提给用户时，`background_results.mention` 才会预留提醒。`data_analysis@2` 和 `document_draft@3` 提供这两个工具。用户的显式评价和从收件箱状态推得的评价现在分别记录来源。

## 首次停止与恢复

首次核对共享接口时，BASE 中有三处 `DeliveryRepository.ack` 调用。旧卡只列出了 API 和场景测试，漏掉了演示脚本。按当时的停止条件，提交 `fc9e3471` 只记录了基线和停止原因。三处调用在 BASE 的位置如下。

| 调用方 | BASE 行号 | 本次处理 |
| --- | ---: | --- |
| `src/harness/api/proactive.py` | 90 | 从请求中的评价值或收件箱状态确定来源 |
| `tests/harness_runtime/scenarios/test_proactive.py` | 189 | 显式传入 `useful`，来源为 `explicit` |
| `scripts/harness/proactive_data_card_demo.py` | 79 | 同样补上 `label_source="explicit"`。脚本差异仅此一行，保留第 78 行的 `read` 调用，后文继续使用 `card` |

修订后的卡片列出了这三处调用。本次在同一 worktree 的 `fc9e3471` 后继续，按派发要求仍用 C0 的指定提交作为 BASE。沿用的[基线记录](p2-C4-baseline.txt)为 `740 passed in 89.76s`，失败清单为空。

## 实现与兼容

- `DeliveryService.read` 在可见性检查后始终返回正文。普通读取的 `notice` 为 `read`，用户明确请求的读取为 `requested`。`mention` 检查当前可见性，再用仓库现有的跨渠道去重键预留。第一次预留将 `notify` 转为 `digest`。已经 `ack` 或预留过则返回 `already_reserved`。返回值包含 `notice` 和 `valid_until`。
- 网关把 `mention` 加入原有的缓存前可见性检查。撤销订阅后，即使相同逻辑键曾成功，调用仍返回 `denied`。Proactive 和未授予该工具的旧能力包也会被拒绝。连接器声明 `read_only=True`、`side_effects=False`、`idempotent=True`，预留只写 Harness 内部的去重记录。
- `data_analysis@2` 在 v1 工具集合上增加 `read` 和 `mention`，`document_draft@3` 在 v2 上增加 `mention`。Main 只要获准使用任一工具，就可以收到相关指针。两份新 Prompt 共用相同的提起说明。共享 Proactive Prompt 说明单订阅、Main 对话隔离、来源与记忆的可信边界，以及按能力包产出并提交校验的职责。数据卡专属指令放回 `data_card.md`。
- **旧版本的兼容边界。** `document_draft@2` 仍可读取后台结果，但共享 `read` 已改为只读，旧 Prompt 中默认读取会预留提醒的说明随之失效。需要主动提起后台结果的新文档任务应选 `document_draft@3`。`data_analysis@1` 保持原来的工具集合和上下文行为。

## 共享接口与反馈

| 接口 | 调用和持久化行为 |
| --- | --- |
| `DeliveryRepository.ack(scope_id, item_id, status, label, label_source, now)` | 新增必传的 `label_source: str \| None`。给出评价值时，任何状态都记 `explicit`。仅有 `handled` / `ignored` 时分别映射到 `useful` / `not_useful`，来源为 `behavior`。仅有 `read` 时不写反馈，也不覆盖已有评价。带标签的 `ack` 沿用原有回填时间及收件箱去重机制。 |
| `ProactiveSubscriptionRepository.label_decision(scope_id, subscription_id, task_id, label, now)` | 新的 `POST /subscriptions/{subscription_id}/decisions/{task_id}/label` 从鉴权身份取得 scope，返回更新后的准入记录。按卡片“只写”两个字段的要求，仅更新 `backfill_label` 和 `label_source="explicit"`。保留约定签名中的 `now`，此路由不更新 `backfilled_at`、收件箱或去重表。 |

两种仓库都在锁保护下更新。PostgreSQL 独立评价在同一事务按 scope、订阅和任务匹配行，并更新该行的 JSONB。跨 scope 或任务不属于该订阅时返回 404。沿用原表结构。

## 红灯、定向回归和演示

红灯阶段只加入测试和卡片允许的断言改动，`src/`、`scripts/` 与 BASE 相同。WSL 导出的测试树为 `1793cc30dc984fae91a835ea12d9352299274504`。运行下方用例得到 `19 failed, 2 passed in 29.35s`。失败包括普通读取仍为 `reserved`、缺少 `mention` 和新版能力包、`ignored` 后来源为空、评价路由返回 404、共享 Prompt 仍含 `data card`。两项通过的是两种仓库原有的损坏数据卡校验用例。

```bash
python -m pytest tests/harness_runtime/scenarios/test_delivery_feedback.py \
  tests/harness_runtime/scenarios/test_proactive.py::test_data_card_end_to_end_and_tamper_failure \
  tests/harness_runtime/unit/test_verifier.py::test_main_profile_registry \
  -q -n0 -o addopts="" -m "integration or not integration" --tb=short
```

实现后的定向检查覆盖卡片要求的八个具名测试。仓库行为使用现有 `repos` fixture 同时在内存和 PostgreSQL 执行，包含只开放 `mention` 时仍注入指针、缓存成功后撤销订阅、静默评价跨 scope 或订阅被拒绝，以及评价前后去重记录数为零。

```text
.....................                                                    [100%]
21 passed in 29.98s
```

另用一张含 3 行数据的 SQLite 表运行卡片补列的离线演示脚本。`acceptance.json` 中 `backfill_label` 为 `useful`，`label_source` 为 `explicit`，注入了 1 个指针。对两版新增 Prompt 还逐项核对了旧文本和新增段落。

```text
{"status": "succeeded", "verification": "passed", "tables": 1, "findings": 2, "output": "/home/xue/harness-p2-C4-check.3hM6pD/demo.a2MTbK/output"}
```

## 提交前三项检查

WSL 2 Ubuntu-22.04，Python 3.11.15，环境为 `/home/xue/venvs/docker-harness-review`。定向与全量回归使用同一份 Git 索引快照 `4688f309e8500609f63b5f575a7560ce8e673836` 导出的原生 Linux 目录。PostgreSQL fixture 使用 `postgres:16` 和各次运行独立的临时容器。原始日志位于 `/home/xue/harness-p2-C4-check.3hM6pD/` 下的 `red-pytest.txt`、`candidate-pytest.txt` 和 `final-pytest.txt`。

### 1. Harness 全量回归

```bash
python -m pytest tests/harness_runtime -q -n0 -o addopts="" -m "integration or not integration"
```

```text
........................................................................ [  9%]
........................................................................ [ 19%]
........................................................................ [ 28%]
........................................................................ [ 38%]
........................................................................ [ 47%]
........................................................................ [ 57%]
........................................................................ [ 66%]
........................................................................ [ 76%]
........................................................................ [ 85%]
........................................................................ [ 95%]
....................................                                     [100%]
756 passed in 100.13s (0:01:40)
```

pytest 退出码为 0。相较 BASE 新增 16 个通过用例，失败清单仍为空。

### 2. 防御性写法检查

```bash
git diff 5d765337222ebc2bc87d8b50123a0b6bd07eea9a -- 'src/harness/*.py' ':!src/harness/contracts.py' ':!src/harness/interfaces.py' ':!src/harness/storage/repositories.py' \
  | grep -nE '^\+.*(except Exception|except:|getattr\([^)]*,[^)]*,|hasattr\(|self\._\w+ is not None)|^\+\s+def __init__\([^)]*=None|^\+ {8,}(from \.|import )'
```

```text
97:+        elif self._delivery is not None and any(
```

该匹配来自 `src/harness/context/builder.py:121`。BASE 同行已有 `elif self._delivery is not None and any(s.name == "background_results.read" for s in tools):`。C4 只扩展了 `any(...)` 中的工具集合，保留原有可选交付服务的判断。

### 3. `src/` 新增行数

```bash
git diff --numstat 5d765337222ebc2bc87d8b50123a0b6bd07eea9a -- src/ ':!src/harness/prompts' ':!src/harness/capabilities' | awk '{s+=$1} END {print s}'
```

```text
101
```

C4 上限 200 行。演示脚本、网关和 `test_verifier.py` 在各自授权的位置各改一行，白名单变更仅涉及 `main_tools@1`。

## 参考文档与任务卡的范围

记忆计划 §7、§8.1 的反馈和预留规则与 C4 一致。§8.2 还描述了过滤静默或取消的指针、按来源哈希判断数据复核结果是否有效。这些工作在任务卡中分配给 C5。C4 按卡片完成读取、预留和独立评价，保持 `materials()` 的既有筛选规则。
