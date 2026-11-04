# DAClaw × SpreadsheetBench 2

直接调用 DAClaw `run_agent.AIAgent`，无需 SWE-agent、Atropos 或 Web 服务。
支持四类任务的生成；前三类用 benchmark 原版 `process_single_item` 评分，未修改官方 evaluator。

## 环境

```bash
cd /home/yangkuntao/DaraOS/DAClaw
# 新机器使用 Python >= 3.11：
python3.11 -m venv venv
source venv/bin/activate
pip install -e . openpyxl tqdm
# Linux 另需安装 LibreOffice（正式公式评分依赖缓存重算）。
```

当前机器已经建好 `venv`，复用现有 demo conda 环境中的依赖；由于网络不可用，
openpyxl 暂时复用本机 3.0.7。联网后执行 `pip install 'openpyxl>=3.1,<4'` 更新。
原系统 `python` 是 3.8，不要用它运行 DAClaw。

正式数据不包含在当前 benchmark 仓库中。按 benchmark README 下载
`KAKA22/SpreadsheetBench-v2`，将每类的 `dataset.json` 和 `spreadsheet/` 放入：

```text
/home/yangkuntao/DaraOS/SpreadsheetBench-2/data/spreadsheetbench-v2/
  Debugging/dataset.json
  Financial_Model/dataset.json
  Template/dataset.json
  Visualization/dataset.json
```

如数据在其他位置，所有命令均可指定 `--dataset /absolute/category/path`。
数据文件须为 JSON 数组，字段沿用官方的 `id`, `instruction`, `spreadsheet_path`,
`golden_response_path`, `answer_position`；输入/答案路径相对类别目录。

## 无模型验证

```bash
source venv/bin/activate
python environments/benchmarks/spreadsheetbench2/smoke.py
python -m unittest discover -s environments/benchmarks/spreadsheetbench2 -p 'test_*.py' -v
```

Smoke 使用真实 AIAgent、OpenAI SDK、terminal 工具和真实 xlsx；仅模型 HTTP 响应由
HTTPX MockTransport 提供，不启动端口、不请求外部服务。覆盖两个任务并发、工具回合、
断点续跑不重复请求、官方正例满分和错误答案降分。人工构造的 smoke 数据仅验证接口，
不代表正式 benchmark 的模型表现。结果在 `outputs/ssb2-smoke/run/evaluation.json`。

## 配置模型

```bash
cp environments/benchmarks/spreadsheetbench2/model.example.json environments/benchmarks/spreadsheetbench2/model.json
# 编辑 model.json 中 model 和 base_url；base_url 包含 /v1。
export SSB2_API_KEY='你的 API key'
```

密钥仅从 `api_key_env` 指定的环境变量读取，不写入配置文件。
默认 OpenAI-compatible Chat Completions，须支持 tool calling 和 SSE 流式响应；`request_overrides` 可配置
供应商所需的额外参数。每个任务独立 Agent、工作目录、DACLAW_HOME 和子进程。

可直接用 `bash environments/benchmarks/spreadsheetbench2/run.sh <子命令> ...`，自动选择本项目 venv。

## 正式运行

```bash
B=environments/benchmarks/spreadsheetbench2/runner.py
python "$B" doctor --category Debugging
# 官方要求先重算输入与标准答案（会原位更新数据集，建议保存一份原始下载备份）：
python "$B" recalculate-dataset --category Debugging
# 先跑一个任务确认模型兼容性：
python "$B" run --category Debugging --limit 1
python "$B" evaluate --category Debugging --limit 1
# 再跑整类；默认输出目录 outputs/spreadsheetbench2/Debugging：
python "$B" run --category Debugging --workers 4 --timeout 1800 --resume
python "$B" evaluate --category Debugging
```

将类别改成 `Financial_Model` 或 `Template` 即可；每个类别使用独立输出目录。
`--ids id1,id2` 可指定任务。自定义 `--output` 时，生成和评分必须使用同一个目录及任务筛选。
比较模型时为每个模型指定新的 `--output`，不要混用已存在的结果。
`prepare` 可在不加载模型的情况下验证数据并生成任务目录和 prompt。
`doctor` 未就绪时返回非零状态；模型调用/缺失/损坏输出/超时也会返回非零状态。
评分命令执行成功返回零，得分看 `evaluation.json`，零分不表示评分器执行失败。

默认评分前通过独立 LibreOffice profile 重算输出，以保留官方数值评分语义。
`--no-recalculate` 只用于常量 smoke 或已完成重算的输出，不能代替正式公式缓存重算。
输入和标准答案仍须按官方要求先完成重算。

每题记录：`tasks/<id>/agent.log`, `trajectory.json`, `status.json`。
标准输出：`output_excel/<id>_output.xlsx`；汇总：`run_summary.json` 和 `evaluation.json`。
续跑仅跳过状态成功、输出存在且任务/输入/模型配置指纹一致的任务。

## Visualization

```bash
python "$B" run --category Visualization --output outputs/ssb2-visual
```

官方图表评分依赖 **Windows Excel/WPS COM + 单独的 VLM 模型**，Linux 上不声称完成此项评分。
将 `output_excel` 拷到 Windows，按 benchmark README 执行：

```bash
python evaluation/run_visual_vlm_checklist_eval.py --tasks-json data/spreadsheetbench-v2/Visualization/dataset.json --output-dir /path/to/output_excel --model glm-4.6v
# 单独设置 VLM_API_KEY。
```

## 运行边界

当前接入使用 DAClaw local terminal 后端：每题只暂存自己的输入，prompt 不包含答案或评分位置，
但进程目录隔离不是文件系统沙箱。模型生成代码以当前用户权限运行，仍可访问主机其他可读文件。
需要严格防答案泄露的正式对比时，应在没有挂载标准答案的独立容器/用户环境中运行生成，
再在宿主机用本适配器评分。`golden_response_path` 只供宿主评分器读取。
