# DAClaw 两个 benchmark 的启动说明

本分支使用普通目录打包 DAClaw、PersonaMem-v3、SpreadsheetBench-2 和 bench_output。
不依赖原机器的目录路径、虚拟环境或 Git 子模块。默认模型为 `gpt-5.4`，基础地址为
`https://api2.aigcbest.top/v1`（实际请求 `/chat/completions`）；密钥由使用者提供。

## 安装

```bash
git clone --single-branch --branch yangkuntao https://github.com/DA-Open/DAClaw.git
cd DAClaw
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-rollout.txt
sudo apt-get update
sudo apt-get install -y libreoffice-calc
```

必须安装 Calc 组件，仅有 LibreOffice Draw/Impress 不能重算 xlsx。Python 要求 3.11+。
此处的根目录包含 `rollout.py`，内层 `DAClaw/` 才是 harness 的源码目录。

## 生成两个 benchmark 的新轨迹

```bash
read -rs -p 'Model API key: ' BENCH_API_KEY; echo
export BENCH_API_KEY
python rollout.py --bench all --limit 3
unset BENCH_API_KEY
```

默认同时跑两个 benchmark，各 3 个样本。自定义接口或模型：

```bash
python rollout.py --bench all --limit 3 \
  --model gpt-5.4 --base-url https://api2.aigcbest.top/v1 \
  --api-key-env BENCH_API_KEY
```

`--base-url` 也支持完整 `/v1/chat/completions` 地址。密钥不会写入配置文件。
模型调用会产生费用；网络和模型权限由使用者自己的 endpoint/key 提供。

只跑一个 benchmark 或查看命令而不调用模型：

```bash
python rollout.py --bench persona --limit 3
python rollout.py --bench spreadsheet --limit 3
python rollout.py --bench all --dry-run
```

可指定 `--output-root /your/output` 与 `--run-name experiment1`。
每次运行使用新目录；若目录已存在，换一个 run-name，避免覆盖之前的结果。
SpreadsheetBench-2 默认只提供 3 条公开示例，limit 不得大于 3。

## PersonaMem-v3 的数据、评分与输出

使用 persona 8 的前 3 个 `personalized_recommendation` 官方实例。
复用随仓库提供的 `bench_output/personamem_v3_gpt54/data/backend`，不需要下载全量数据。
若该数据目录被移除，runner 会从 `bowen-upenn/PersonaMem-v3` 下载所需 persona 文件。
可用 `--data-endpoint https://huggingface.co` 切换下载站点；默认 hf-mirror.com。

Agent 仅通过 `read_file` 和 `search_files` 访问官方时间截断、去除评分字段的历史快照。
每题独立进程，无法通过这两个工具读取快照外的标准答案与评分元数据。
使用上游 `personalized_recommendation_prompt` 和
`compute_personalized_recommendation_metrics`；不调用额外 LLM judge。

新结果位于 `bench_output/personamem_v3_rollout_<run-name>/`：

- `summary.json`：平均 tier_concordance、Recall@K、NDCG@K、MRR。
- `results.csv`、`results.json`：逐例分数与模型回答。
- `tasks/001..003/trajectory.json`：完整模型消息、工具调用、结果和 token 用量。
- `tasks/001..003/history/`：该题的时间截断快照。

## SpreadsheetBench-2 的数据、评分与输出

使用官方 2026-09-10 ZIP release 中 Debugging 类前 3 题：01_01、01_02、01_03。
预置选定题目的输入与标准答案；新运行会复制为独立数据副本，保留已发布数据和分数。
若删除预置数据，脚本下载官方 `KAKA22/SpreadsheetBench-v2` 的约 134 MB release ZIP，
仅解压所选 3 题，记录下载 URL 与原始 SHA256。

按官方要求，输入、标准答案、模型输出均用 LibreOffice Calc 重算。
评分直接调用未修改的 `SpreadsheetBench-2/evaluation/evaluation.py`。
模型生成时只暂存自身输入；本地 terminal 按当前用户权限运行，目录独立不等于严格沙箱。
正式防答案泄露的模型对比应在仅挂载输入的容器中运行生成，再在宿主评分。

新结果位于 `bench_output/spreadsheetbench2_rollout_<run-name>/`：

- `tasks/<id>/trajectory.json`：完整轨迹；`agent.log`、`status.json`：日志和状态。
- `output_excel/<id>_output.xlsx`：生成并重算的工作簿。
- `run_summary.json`：生成状态；`evaluation.json`：官方逐例 accuracy 与平均 accuracy。
- `model.json`、`run_config.json`：不含密钥的配置。

官方 strict accuracy 要求 regression_accuracy 与 modification_accuracy 都达到 1.0，
其中 regression 使用上游规定的 99.8% 容差。成功生成 Excel 不代表通过官方评分。
本分支不运行 Visualization，其官方评分依赖 Windows Excel/WPS COM 和独立 VLM。

## 查看已记录的六条轨迹

- PersonaMem-v3：`bench_output/personamem_v3_gpt54/tasks/001..003/trajectory.json`。
  3/3 完成，平均 tier_concordance 0.4577。
- SpreadsheetBench-2：`bench_output/spreadsheetbench2_gpt54/tasks/01_01..01_03/trajectory.json`。
  3/3 完成，官方 strict accuracy 0/3；三条标准答案正对照均为满分。

已记录轨迹保持原始绝对路径用于溯源；新 rollout 不使用旧 request/status 文件启动。
这不是两个 benchmark 的完整成绩。原始下载来源、评分复核和哈希见各运行目录的
`data_provenance.json`、`download_provenance.json` 与 `verification.json`。

## 离线检查

```bash
python -m unittest discover -s DAClaw/environments/benchmarks/personamem_v3 -p 'test_*.py'
python -m unittest discover -s DAClaw/environments/benchmarks/spreadsheetbench2 -p 'test_*.py'
python DAClaw/environments/benchmarks/spreadsheetbench2/smoke.py
```

smoke 使用真实 AIAgent 和工具执行，仅 HTTP 模型响应来自离线 fixture，不代表模型分数。
嵌套项目原 README 保留上游说明；本分支适配后的统一启动方式以本文及根 README 为准。

## 发布范围与许可

五项内容以源码、示例数据和完整轨迹形式提交；不提交嵌套 .git、venv、缓存、重复 ZIP、
本机 Calc 二进制和临时 DACLAW_HOME。环境由安装步骤重建；来源版本见
`PUBLICATION_MANIFEST.json`。
PersonaMem 数据继承 GIST-Bench 的 CC-BY-NC-4.0，保留署名并遵守非商业使用条款；
各项目原有 LICENSE 和数据声明保留。请遵守上游许可。
