# DAClaw + gpt-5.4：两个 benchmark 的启动说明

目标文档位置：`/home/yangkuntao/DaraOS/BENCH_EVALUATION_GUIDE.md`。

两个评测均直接调用本地 `DAClaw/run_agent.py` 的 `AIAgent`，无需启动 Web 服务或 SWE-agent。
模型固定为 `gpt-5.4`，Chat Completions 请求地址为
`https://api2.aigcbest.top/v1/chat/completions`；SDK 配置填写其基础地址
`https://api2.aigcbest.top/v1`，SDK 自动追加 `/chat/completions`。
密钥使用之前提供的同一个密钥，只通过环境变量传入，不写进文档或配置文件。

## 1. 公共环境与密钥

```bash
cd /home/yangkuntao/DaraOS/DAClaw
source venv/bin/activate
read -rs -p '请输入模型 API key: ' BENCH_MODEL_API_KEY
echo
export PM3_API_KEY="$BENCH_MODEL_API_KEY"
export SSB2_API_KEY="$BENCH_MODEL_API_KEY"
unset BENCH_MODEL_API_KEY
```

本机 Python 由项目 `venv` 提供。SpreadsheetBench-2 需要 `openpyxl`、`tqdm`
和带 Calc 组件的 LibreOffice。系统原有 LibreOffice 缺少 Calc，已在
`bench_output/spreadsheetbench2_gpt54/runtime/` 部署独立 Calc 运行时，未修改系统安装。
一键脚本会自动使用此运行时；分步运行时先执行：

```bash
export PATH="/home/yangkuntao/DaraOS/bench_output/spreadsheetbench2_gpt54/runtime/bin:$PATH"
```

模型请求及首次数据下载需要可访问外网。
每次对比使用新的输出目录；不要覆盖先前已完成的结果。

## 2. PersonaMem-v3：3 个例子

本次配置为 persona `8` 的前 3 个 `personalized_recommendation` 实例，
是同一任务类别的 3 条样本，不是完整 benchmark，也不是 3 类任务。
使用官方时间截断快照、提示词及确定性排序评分，不调用额外的 LLM judge。

已有数据可复用，无需重复下载：

```bash
cd /home/yangkuntao/DaraOS/DAClaw
source venv/bin/activate
python environments/benchmarks/personamem_v3/runner.py \
  --benchmark /home/yangkuntao/DaraOS/PersonaMem-v3 \
  --backend-dir /home/yangkuntao/DaraOS/bench_output/personamem_v3_gpt54/data/backend \
  --output /home/yangkuntao/DaraOS/bench_output/personamem_v3_gpt54_rerun \
  --model gpt-5.4 --base-url https://api2.aigcbest.top/v1 \
  --user-id 8 --limit 3
```

首次下载时，删除 `--backend-dir ...` 参数，加入：

```text
--download --data-endpoint https://hf-mirror.com
```

输出：`summary.json` 为平均分，`results.csv`/`results.json` 为逐例分数，
`tasks/001..003/trajectory.json` 包含完整消息、工具调用、工具结果与 token 用量。
主要指标为 `tier_concordance`，另有 Recall@K、NDCG@K 和 MRR。
已完成的第一次运行保存在 `bench_output/personamem_v3_gpt54/`。

## 3. SpreadsheetBench-2：3 个例子

一键下载、检查环境、重算、运行 3 例并评分（没有设置 `SSB2_API_KEY` 时会提示输入）：

```bash
cd /home/yangkuntao/DaraOS/DAClaw
bash environments/benchmarks/spreadsheetbench2/run_three_gpt54.sh
```

该命令也会把本说明复制到 `/home/yangkuntao/DaraOS/BENCH_EVALUATION_GUIDE.md`。
下面给出等效的分步命令。

使用 `Debugging` 类官方数据的前 3 条样本。输入、标准答案和模型输出均按官方要求
通过 LibreOffice 重算，再调用原版 `evaluation/evaluation.py` 评分。
模型配置已保存于 `environments/benchmarks/spreadsheetbench2/model.gpt54.json`，不含密钥。

首次下载（官方以 ZIP 发布，下载 `spreadsheetbench-v2-09-10.zip`，约 134 MB，
仅解压前 3 题的输入与标准答案；同时保存原始元数据及文件哈希）：

```bash
cd /home/yangkuntao/DaraOS/DAClaw
source venv/bin/activate
python environments/benchmarks/spreadsheetbench2/download_subset.py \
  --category Debugging --limit 3 \
  --output /home/yangkuntao/DaraOS/bench_output/spreadsheetbench2_gpt54/data/Debugging
```

准备、生成、评分：

```bash
B=environments/benchmarks/spreadsheetbench2/runner.py
D=/home/yangkuntao/DaraOS/bench_output/spreadsheetbench2_gpt54/data/Debugging
O=/home/yangkuntao/DaraOS/bench_output/spreadsheetbench2_gpt54
C=environments/benchmarks/spreadsheetbench2/model.gpt54.json

python "$B" doctor --category Debugging --dataset "$D" --config "$C"
python "$B" recalculate-dataset --category Debugging --dataset "$D" --limit 3
python "$B" run --category Debugging --dataset "$D" --output "$O" \
  --config "$C" --limit 3 --workers 1 --timeout 1800
python "$B" evaluate --category Debugging --dataset "$D" --output "$O" --limit 3
```

`recalculate-dataset` 会更新下载副本中的公式缓存；首次下载哈希记录在
`download_provenance.json`。重新评测可将 `O` 改为新目录，复用 `D`。
已有生成中途失败时，可在同一配置下给 `run` 加 `--resume`；成功且指纹匹配的任务会跳过。

输出：

- `evaluation.json`：官方逐例 accuracy 与平均 accuracy。
- `run_summary.json`：运行状态，生成成功不代表得分满分。
- `tasks/<id>/trajectory.json`：完整 Agent 轨迹。
- `tasks/<id>/agent.log`、`status.json`：日志与运行状态。
- `output_excel/<id>_output.xlsx`：生成的工作簿。

Debugging 的官方评分可能包含公式、数值等要求，以官方 evaluator 返回字段为准。
本次不运行 Visualization：其官方评分需要 Windows Excel/WPS 与独立 VLM。
当前适配器使用本地 terminal，每题工作目录独立；这不构成严格的主机文件系统沙箱。

## 4. 退出与离线适配器检查

```bash
unset PM3_API_KEY SSB2_API_KEY
python -m unittest discover -s environments/benchmarks/personamem_v3 -p 'test_*.py'
python -m unittest discover -s environments/benchmarks/spreadsheetbench2 -p 'test_*.py'
```

以上单元测试只验证适配器，不能替代真实模型评测。若没有生成 `evaluation.json`，
不要将 SpreadsheetBench-2 视为已评分；检查下载、联网权限和任务日志。
