# 基线结果：webh-gpt61sol-r1

DAClaw 现有 harness 在 DAComp-DA 上的第一组基线，用于对比后续的 harness 改动。2026-11-03 在 137 服务器上运行。

## 配置

| 项目 | 取值 |
|---|---|
| harness | Web Data Agent 路径：`web_server._create_web_chat_agent` + `_da_agent_system_prompt`，工具集 `data-agent-chat`，按原样保留（含 skill 工具） |
| 被测模型 | `gpt-6.1-sol`（OpenAI 兼容中转站，`chat_completions`） |
| 镜像 | `daclaw:v0.3`，commit `aed9da85` |
| 最大轮数 | 沿用产品设置 `agent.max_turns` = 90 |
| 题目 | `dacomp-004`、`014`、`015`（`random.Random(42)` 抽样的前 3 题）+ `dacomp-007`（调试题） |
| judge | 官方 `llm_judge.py` / `get_score.py`，英文版；judge 模型为 `gpt-6.1-sol`，`reasoning_effort=low` |

## 结果

| 题目 | 分数 | 用时（秒） | 模型调用 | token | 图片 |
|---|---:|---:|---:|---:|---:|
| dacomp-004 | 92.0 | 922 | 11 | 244,787 | 2 |
| dacomp-014 | 54.5 | 955 | 14 | 421,839 | 2 |
| dacomp-015 | 89.72 | 1259 | 14 | 458,351 | 1 |
| dacomp-007（调试题） | 78.0 | 1041 | 16 | 496,059 | 2 |

官方汇总（4 题）：完整性 79.39，准确性 70.38，结论性 64.58，可读性 75.0，专业性 100.0，可视化 70.0，**总分 78.56**。不含 007 的 3 题平均为 78.74。

## 使用时注意

- **不能和论文数字比。** judge 不是官方的 `gemini-2.5-flash`，分数只适合在同一 judge、同一配置下比较 harness 改动前后的变化。
- **007 不是测试样本。** 开发时看过这题的参考报告，只用于调试。
- **样本很少。** 3 道测试题只能看出流程是否通、方向大致如何，不能说明统计意义上的差异。
- **每题结尾都调用了 `skill_manage`** 新建一个本题专用的 skill。每次尝试都用全新的 `DACLAW_HOME`，所以这些 skill 不会带到下一题，但会增加用时。

## 文件

- `config.json`、`state.json`：冻结的运行配置和每题的尝试记录
- `summary.csv`：每题一行的汇总
- `scores/`：官方 judge 输出的逐题 CSV 和 `overall_results.csv`
- `results/<id>.json`：每题结果，包含完整消息、工具错误、图片记录、用量和各维度分数
- `agent_results/webh-gpt61sol-r1/<id>/`：官方提交格式，即 `<id>.md`、`<id>-traj.txt`、`figure_NN.png`

每次尝试的原始目录 `attempts/` 没有收录，因为里面有复制出来的 `DACLAW_HOME`，包含 API key。
