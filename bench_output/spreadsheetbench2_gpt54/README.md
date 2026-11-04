# DAClaw + gpt-5.4：SpreadsheetBench-2 三例评测

官方 Debugging 类前 3 个实例（01_01、01_02、01_03），不是全量 benchmark。

3/3 个实例完成生成和评分，输出均存在；官方严格 accuracy 为 0/3（0%）。

| ID | Strict accuracy | Regression accuracy | Modification accuracy |
|---|---:|---:|---:|
| 01_01 | 0% | 100.00% | 15.03% |
| 01_02 | 0% | 100.00% | 0.00% |
| 01_03 | 0% | 98.04% | 2.23% |

分数来自未修改的官方 evaluator。输入、标准答案和模型输出均已通过 Calc 重算。
对每题使用标准答案作为输出的评分正对照均为 100%，并重算核对了保存分数。
官方 strict accuracy 要求 regression 和 modification 两项都达到 1.0（regression 有官方 99.8% 容差）。

轨迹：tasks/<id>/trajectory.json；生成 Excel：output_excel/<id>_output.xlsx；详细评分：evaluation.json / results.csv；运行配置：run_config.json；核验：verification.json。

系统 LibreOffice 原先缺少 Calc；已在 runtime/ 部署独立运行时，未更改系统安装。数据下载源与原始 SHA256 见 data/Debugging/download_provenance.json。
本地 terminal 以当前用户权限执行，工作目录隔离不等于严格文件系统沙箱；已检查轨迹中没有标准答案或评分元数据路径访问。

两个 benchmark 的启动说明：/home/yangkuntao/DaraOS/BENCH_EVALUATION_GUIDE.md
