# P2 C3 执行报告

分支 `feat/harness-p2-analysis-baseline`，BASE `4a7abe38`（#36 之后的主线）。
本卡不调模型：基线全从已校验产物读，复算全用共享只读执行器。

## 做了什么

| 部分 | 内容 |
|---|---|
| `tools/connectors/data.py` | 抽出 `ReadonlySource` / `ReadonlyResult` / `open_readonly` / `run_readonly` / `file_digest`；`data.connect` 在连接时记录 `sha256` 与 `size_bytes`；`data.query` 改走 `run_readonly` |
| `personal/analysis_types.py` | `SourceChangedEvent`、`AnalysisWatch`、`BaselineBody`、`RecomputeResult` 等 |
| `personal/baseline.py` | `BaselineWriter`：`data_analysis@*` 的 Main 任务提交成功后写一条 `ANALYSIS`，证据取自 `query_evidence` 检查项，数据源取自 `data.connect` 动作，回答取 `analysis` 产物的 Answer 一节 |
| `personal/analysis_admission.py` | `decide_analysis`：只读规则判断，`input_snapshot` 带 `baseline_ref`、新旧 `sha256`、`watch` |
| `personal/recompute.py` | `Recomputer.ensure`：按基线重建视图、重跑 SQL、写 `query/<id>.json|csv`、`recompute` 产物与 `recompute_recorded` 事件；幂等；失败也写产物 |
| `tasks/service.py` | `after_commit` 钩子，两条返回分支都调用；钩子异常只记日志 |
| `tasks/proactive_controller.py` | `analysis_watch@1` 的订阅校验与 `source_changed` 分派 |
| `api/proactive.py` | 订阅支持 `analysis_watch@1` + `baseline_ref` / `watch`；事件支持 `source_changed` |
| `context/builder.py` | 订阅分支对 `analysis_watch@1` 先 `Recomputer.ensure`，再组装上下文 |
| 能力包/Prompt | `capabilities/analysis_watch.yaml`；`prompts/analysis_watch.md` 只有 `TODO(C5)` |
| 夹具 | `tests/harness_runtime/fixtures/analysis/build.py`，CSV + `COPY` 写入 `orders.duckdb` |

## 三项检查

### 1. 完整回归

```
python -m pytest tests/harness_runtime -q -n0 -o addopts="" -m "integration or not integration"
2 failed, 908 passed, 12 warnings in 170.04s
```

两条失败都在基线清单里（`docs/plans/p2-C3-baseline.txt`）：
`test_fingerprint_skip_and_diff_after_baseline[postgres]`（`decisions()` 缺 `ORDER BY`，见 #37）与
`test_responses_round_trip_keeps_reasoning_items`（openai>=2.5x 已知）。

### 2. 防御性写法检查

命中的一处已在报告里说明：

```
+    def __init__(self, repos, tasks, policy, *, memories=None, scope_limit=2, clock=utcnow):
```

`ProactiveController` 的 `memories` 保留默认 `None`：本卡只允许改分派处，而现有测试
（`scenarios/test_proactive.py` 等）不传该参数；改成必填会需要改本卡未列出的测试文件。
`analysis_watch` 分支在 `memories is None` 时会抛 `ValueError("analysis_watch needs the memory service")`，
不会静默跳过校验。

另外两处已改掉：`builder` 里的 `self._recomputer is not None`（`recomputer` 现在是必填参数）、
`open_readonly` 里的 `except Exception`（改为 `except (duckdb.Error, ValueError, OSError)`）。

### 3. 行数

```
git diff --numstat <BASE> -- src/ ':!src/harness/prompts' ':!src/harness/capabilities' | awk '{s+=$1} END {print s}'
207
```

上限 620。

## 测试

新增 24 条：执行器/夹具 4 条、基线/订阅 7 条（内存 + PostgreSQL 各跑一遍）、复算 6 条，
其余为原有回归。关键数字与卡一致：

| | v1 | v2 |
|---|---:|---:|
| 3 月已完成订单数 | 169 | 169 |
| 缺价行 | 27 | 0 |
| 销售额 | 219,912.81 | 232,944.31 |

复算得到 `delta=13,031.50`、`relative_change≈5.93%`、`exceeds_threshold=True`。

## 与本卡的偏差

1. **提交了并开了 PR**：卡里「只在本地提交，不 push，不开 PR」和 `Co-Authored-By: Codex` 是给
   Codex 的指令；本仓库的协作约定是每张卡单独开 PR 到 `DaraOS`，因此按后者执行。
2. **`recomputer` 必填，连带的构造点有 9 处**（卡里列了 7 处）：多出的是 #36 新增的
   `test_approval_resume.py` 和 `test_runner.py` 的第二个构造点；都传 `NoRecompute()` 替身。
3. **`_register` 没有整体改成调用 `open_readonly`**：它仍负责生成会话重放用的 `setup` 语句
   （`data.connect` 换文件时重建目录用），只把「列级视图创建」抽成 `_create_source_views`
   与 `open_readonly` 共用，`data.query` 改走 `run_readonly`。`test_data_connector.py` 原样通过。

## 留给 C5 的接口

- `recompute` 产物：每条查询 `{query_index, query_ref, old_values, new_values, row_count}`，
  另加 `threshold` 与 `error`；`threshold.relative_change` 在 `old == 0` 时为 `None`。
- 复算写出的 `query/<id>.json` 与 `data.query` 同格式，`recompute_replay` 可直接按 `query_ref` 读 SQL 重跑。
- `open_readonly` / `run_readonly` 就是 C5 复算校验要用的那份实现。
