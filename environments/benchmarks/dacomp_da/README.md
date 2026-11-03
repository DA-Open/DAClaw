# DAComp-DA 评测接口

用 DAClaw Data Agent 跑 [DAComp-DA](https://github.com/ByteDance-Seed/DAComp)，并用官方 `llm_judge.py` / `get_score.py` 打分。

- `bench.py`：宿主机调度（只用标准库）。每题每次尝试起一个全新的 `daclaw` 镜像容器。
- `run_task.py`：容器内执行。连接只读 SQLite，调用 `AIAgent`，把图表导出成 PNG，写出官方提交物。

## 用法（在服务器上）

```bash
cd ~/DAClaw/environments/benchmarks/dacomp_da
python3 bench.py sample  --n 10 --seed 42 --out ~/dacomp_bench/tasks_rand10_seed42.txt
python3 bench.py run     --run-name rand10-gpt61sol --tasks ~/dacomp_bench/tasks_rand10_seed42.txt --workers 3
python3 bench.py judge   --run-name rand10-gpt61sol
python3 bench.py summary --run-name rand10-gpt61sol
```

前置条件：
- 镜像 `daclaw:v0.3`
- `~/.daclaw/config.yaml` 和 `.env`（模型和 key 从这里读）
- 官方仓库放在 `~/dacomp_bench/DAComp`，至少要有 `dacomp-da/evaluation_suite` 及其 `src/` 下的 rubric 和参考报告
- judge 用的独立 venv 在 `~/dacomp_bench/judge-venv`，装有 loguru、tenacity、openai、pandas、requests、httpx

题目数据库从 `HF_ENDPOINT`（默认 hf-mirror.com）按需下载，缓存在 `~/dacomp_bench/data/`。

## 产出

```
~/dacomp_bench/runs/<run>/
├── config.json                 运行配置（镜像 ID+commit、模型、迭代上限、超时、题目清单）
├── state.json                  每题状态 + 每次尝试记录（断点续跑依据）
├── agent_results/<run>/<id>/   官方提交格式：<id>.md、<id>-traj.txt、figure_NN.png
├── attempts/<id>/<n>/          每次尝试的原始目录（容器日志、meta.json、messages.json、DACLAW_HOME）
├── results/<id>.json           每题结果，字段对齐组里 GDPval 产出
├── scores/                     官方 judge CSV + overall_results.csv
└── summary.csv                 每题一行：状态、用时、调用次数、token、图片数、分数
```

`results/<id>.json` 的字段和 GDPval 一致：`task_id`、`turns_used`、`finished_naturally`、`messages`、`tool_errors`、`grade`、`score`、`reward`、`passed`、`error`、`elapsed_seconds`。

和 GDPval 不同的地方：
- `score` 是官方加权总分（0–100），`reward = score / 100`。
- `grade` 里是六个维度分数，以及 judge 的原始评语。
- DAComp 没有通过线，`passed` 恒为 `null`。

## 隔离与可复现

- 每次尝试使用全新的 `DACLAW_HOME`：只复制 `config.yaml` 和 `.env` 里列出的 key（默认只有 `OPENAI_API_KEY`），skill 从镜像自带版本重新同步。session、memory 和 skill 的修改不会带到下一题。
- 题目数据库以只读方式挂载。连上之后，DuckDB 关闭外部文件访问并锁定配置，agent 的 SQL 读不到容器里的其他文件。
- gold rubric 和参考报告不挂进容器，只有 judge 阶段在宿主机读取。
- `config.json` 记录了镜像 ID 和 commit、模型、prompt 版本等配置。同一个 run 名下这些配置变了会拒绝续跑。

## 失败处理

- **超时**：默认单题 2 小时。超时只用来防止卡死，agent 的工作量由 `--max-iterations` 控制。默认值 0 表示沿用产品设置，即 `config.yaml` 里的 `agent.max_turns`（目前是 90）。
- **超时或崩溃后**：从 DAClaw 每轮写盘的 session 日志里恢复部分轨迹，写成 `<id>-traj.txt`。`<id>.md` 写入占位文本，judge 仍会打分（按失败计）。
- **重试**：只对 API 故障和容器故障重试，最多 `--max-retries` 次。超时、答不出、答得不好都不重试，不会挑选结果。每次尝试都保留。
- **断点续跑**：同一个 run 名重跑时，跳过状态为最终状态且提交物完整的题。

## 已知问题与待确认

1. **用时长**。`gpt-6.1-sol` 是推理模型，一次给出完整题目时，agent 会自己规划很深入的分析。冒烟测试 `dacomp-007` 跑了 30 分钟，只做了约 23 次工具调用，仍未完成。之前用 DeepSeek 在 CLI 或 Web 上分轮提问，每轮只要几分钟。可调的有两处：config 里的 `reasoning_effort`，以及换模型。
2. **`skill_manage` 很慢**。这是 DAClaw 本身的行为，不是本接口的 bug。agent 每保存一次 skill，都要经过两次 LLM 质量校验（`tools/skill_manager_tool.py`）；辅助模型是 `provider: auto`，也就是同样用推理模型。冒烟测试里它改了两次 `jupyter-live-kernel`，每次约 276 秒，而评测工具集里根本没有 Jupyter。评测按原样保留了这个工具，分数反映的是现有 harness 的真实表现。是否要优化，留给 harness 改造讨论。
3. **judge 不是官方模型**。目前 judge 用的是中转站的 `gpt-6.1-sol`，并设了 `reasoning_effort=low`，官方示例用的是 `gemini-2.5-flash`。用默认推理强度时，单次生成常超过中转站约 100 秒的网关超时，返回 HTTP 524，流式返回也没用。分数可以用来比较 harness 改动前后，但不能直接和论文里的数字比。
4. **被测模型**。目前先用 `gpt-6.1-sol`。之后可以在 137 上用 vLLM 部署 Qwen，或者接组里现成的部署，只需要改 `--model` 和 config。
5. **图表 PNG**。`data_visualize` 和 `data_dashboard` 生成的 ECharts HTML，会由 headless Chromium 导出成 `figure_NN.png`，工具结果里会返回 `markdown_embed` 供报告引用。能不能引用上，取决于模型是否遵守 prompt 的要求；引用了不存在的图片会记录在 `missing_image_refs`。
6. **只跑英文版**。中文版 `dacomp-zh` 需要另外复制 `src_zh` 并使用 `get_score_zh.py`，目前还没接。
