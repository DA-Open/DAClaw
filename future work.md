# Future Work

本文档用于总结 DAClaw 当前架构、DataAgent 适配现状，以及后续可探索的改进方向。内容基于当前项目结构、已有 DataAgent 相关模块，以及 DAClaw 从 OpenClaw / Hermes 演进而来的架构特点整理。

## 1. 当前项目定位

DAClaw 目前可以理解为一个通用 Agent Runtime 加多个外围入口和能力模块的系统。

它不是单纯的 CLI 工具，而是一个支持多入口、多平台、可插拔工具、会话持久化、Profile 隔离、RL 训练和数据分析能力的 agent 框架。

整体可以概括为：

```text
DAClaw = 通用 Agent Runtime + 工具系统 + 多入口适配 + 状态/配置系统 + RL/评测扩展
```

如果进一步面向 DataAgent 方向演进，则更适合定位为：

```text
DAClaw = 通用 Agent Runtime
DataAgent = DAClaw 上的一条专用数据分析 runtime / mode
```

也就是说，不建议直接把整个 DAClaw fork 成一个完全独立的数据系统，而是保留 DAClaw 的通用 agent 能力，在其上增加面向数据任务的专门适配层。

## 2. 当前架构概览

### 2.1 用户入口层

当前 DAClaw 有多个入口：

```text
daclaw_cli/main.py      # daclaw 命令入口
cli.py                  # 交互式 CLI
gateway/run.py          # Telegram / Discord / Slack / WhatsApp 等消息平台入口
acp_adapter/            # VS Code / Zed / JetBrains 等编辑器集成
batch_runner.py         # 批处理入口
cron/                   # 定时任务
rl_cli.py               # RL 训练专用 CLI
environments/           # RL / benchmark / evaluation 环境
```

这些入口最终大多会进入统一的 Agent Runtime，也就是 `AIAgent` 或训练环境中的 `DAClawAgentLoop`。

### 2.2 Agent Runtime 层

核心是 `run_agent.py` 中的 `AIAgent`。

主流程是：

```text
用户输入
  ↓
入口层：CLI / Gateway / ACP / Batch / RL
  ↓
AIAgent.run_conversation()
  ↓
构建 messages + system prompt + tool schemas
  ↓
调用 OpenAI-compatible chat completions
  ↓
模型返回：
  ├─ 普通文本：结束并返回 final_response
  └─ tool_calls：
       ↓
       model_tools.handle_function_call()
       ↓
       tools.registry dispatch
       ↓
       工具返回 JSON 字符串
       ↓
       追加 tool message
       ↓
       继续下一轮模型调用
```

这个设计的优点是简单、统一、容易理解。缺点是当系统变成多平台、多任务、多模式时，`run_agent.py` 容易变得过大，DataAgent、RL、Gateway 等不同场景的逻辑可能逐渐挤进同一个主循环。

### 2.3 工具系统

工具系统由三部分组成：

```text
tools/registry.py
  ↑
tools/*.py 在 import 时调用 registry.register()
  ↑
model_tools.py 收集 schema、过滤工具、处理函数调用
  ↑
run_agent.py / cli.py / batch_runner.py / environments 使用
```

新增普通工具的基本路径是：

1. 在 `tools/` 下创建新工具文件。
2. 调用 `registry.register(...)` 注册工具。
3. 在 `toolsets.py` 里加入对应 toolset。

目前 DataAgent 相关工具已经通过这个机制接入。

### 2.4 Toolset 系统

`toolsets.py` 定义了不同工具集合，例如：

```text
web
file
terminal
browser
data
rl
memory
delegation
daclaw-full / daclaw-cli / daclaw-gateway 等
```

其中 data toolset 当前包含：

```text
data_profile
data_catalog
analysis_audit
```

并且这些工具目前也被放进了 `_DACLAW_CORE_TOOLS`，这说明数据能力已经默认进入 DAClaw 的核心工具集合。

### 2.5 配置与 Profile

DAClaw 支持多 Profile 隔离。

关键原则：

```text
代码路径使用 get_daclaw_home()
用户展示路径使用 display_daclaw_home()
不要写死 ~/.daclaw
```

这对于 DataAgent 很重要。因为 DataAgent 未来会产生更多长期状态，例如：

```text
data_catalog/
analysis_runs/
data_artifacts/
dataset_profiles/
metric_definitions/
warehouse_connections/
```

这些状态都必须跟随 active profile 隔离。

### 2.6 CLI / Gateway / ACP

CLI 通过 `daclaw_cli/commands.py` 的 `COMMAND_REGISTRY` 统一注册 slash commands。

Gateway 则负责把 Telegram、Discord、Slack、WhatsApp 等平台消息转换成 agent 会话。

ACP Adapter 用于编辑器集成。

这些入口不应各自实现 DataAgent 逻辑，而应调用同一个 DataAgent mode / service。否则 DataAgent 能力会在 CLI、Gateway、Web、ACP 中重复实现，后续维护成本会很高。

### 2.7 RL 与 Environments

RL 相关结构主要包括：

```text
rl_cli.py
tools/rl_training_tool.py
environments/daclaw_base_env.py
environments/agent_loop.py
environments/tool_context.py
environments/*_env/
```

训练环境中的 agent loop 与 `run_agent.py` 的普通对话 loop 思路一致：

1. 模型生成 tool call 或 final answer。
2. 工具执行。
3. 环境根据结果计算 reward。
4. Atropos / Tinker 接收轨迹和 reward 进行训练。

这套机制非常适合后续训练 DataAgent 的数据分析行为。

## 3. DataAgent 当前状态

当前项目中已经有一些 DataAgent 相关能力。

### 3.1 数据源 Profiling

相关文件：

```text
agent/data_sources.py
tools/data_analysis_tool.py
```

当前能力：

- 定义了 `DataSourceAdapter` 抽象。
- 实现了 `CsvAdapter`。
- 支持 CSV / TSV profiling。
- 可以统计字段名、缺失值、唯一值、数值 min/max/mean、样例行。

当前限制：

- 仅支持本地 CSV / TSV。
- 不支持 Excel、Parquet、SQLite、DuckDB、Postgres、BigQuery、Snowflake 等常见数据源。
- profiling 结果目前还不是一个完整的数据资产对象。

### 3.2 数据目录 Catalog

相关文件：

```text
agent/data_catalog.py
tools/data_catalog_tool.py
```

当前能力：

- 在 profile-scoped `DACLAW_HOME/data_catalog` 下持久化数据目录。
- 支持 table 记录。
- 支持 relation 记录。
- 支持 search、get、upsert、summary。

当前限制：

- Catalog 目前主要存表、字段、关系。
- 还没有 metric definitions。
- 还没有 business glossary。
- 还没有 dataset lineage。
- 还没有 artifact provenance。
- 还没有和实际 query / analysis run 深度绑定。

### 3.3 分析报告审计

相关文件：

```text
agent/analysis_audit.py
tools/analysis_audit_tool.py
run_agent.py
```

当前能力：

- 能识别数据分析类任务。
- 能把 conversation trajectory 整理成 audit-friendly trace。
- 能用辅助 LLM 审计 final report 是否忠实于分析过程。
- 如果 audit 失败，`run_agent.py` 会自动追加 repair prompt，让 agent 修复后再返回。

这是目前 DataAgent 方向最有价值的已有能力之一。

当前限制：

- 主要审计 final report。
- 对 SQL、Python、DataFrame、chart、intermediate artifact 的结构化追踪还不足。
- 无法稳定验证数值是否真实来自可复现执行。

### 3.4 当前 DataAgent 形态总结

目前 DAClaw 中的 DataAgent 更像是：

```text
通用 agent + data tools + final report audit
```

还不是完整的：

```text
面向数据任务生命周期的 DataAgent Runtime
```

也就是说，DataAgent 已经有工具和局部 guardrail，但还缺少独立的任务模式、分析生命周期、artifact 管理、数据源管理和数据任务评测体系。

## 4. 为什么需要单独适配 DataAgent

OpenClaw / Hermes 的通用 agent 架构适合：

- 通用聊天
- 工具调用
- 编程任务
- 浏览器任务
- 多平台消息交互
- 长上下文对话

但 DataAgent 有更强的领域约束：

1. 数据源需要连接、采样、profiling、权限隔离。
2. 数据 schema 和业务语义需要长期记忆，但不能混入普通 memory。
3. 分析任务需要可追溯执行过程。
4. 指标口径必须稳定。
5. final report 必须有证据链。
6. SQL / Python / chart / dataframe 都应成为 artifact。
7. 需要专门处理数据质量、join 粒度、时间范围、过滤条件、异常值、缺失值、重复值等问题。
8. 评测和 RL reward 不能只看最终文本，而要看分析过程和数值正确性。

所以 DataAgent 不应该只是加几个工具，而应该有独立的架构层。

## 5. 建议的 DataAgent 架构

建议新增一个 DataAgent 专属包：

```text
agent/data_agent/
  __init__.py
  mode.py              # DataAgent mode / runtime configuration
  prompts.py           # DataAgent system/developer prompts
  workflow.py          # 数据分析生命周期编排
  sources.py           # 数据源连接与适配，或复用 agent/data_sources.py
  catalog.py           # semantic catalog 封装，或复用 agent/data_catalog.py
  artifacts.py         # query/result/chart/report artifact 管理
  validators.py        # 数值、schema、join、report 校验
  provenance.py        # 证据链和 lineage
  metrics.py           # 指标定义与口径管理
```

这个包不应该替代 `run_agent.py`，而是被 `AIAgent`、CLI、Gateway、RL 环境调用。

### 5.1 DataAgent 生命周期

建议把 DataAgent 的任务流程定义为：

```text
1. Data Intake
   接收用户问题、数据源、业务背景、期望输出。

2. Source Profiling
   识别文件/数据库/API/warehouse，抽样并生成 profile。

3. Semantic Catalog
   保存表结构、字段含义、主键、粒度、枚举值、表关系、指标口径。

4. Analysis Planning
   生成分析计划：需要哪些表、哪些字段、如何 join、如何过滤、计算哪些指标。

5. Execution
   使用 SQL/Python/DuckDB/沙箱执行分析。

6. Validation
   校验数值、样本量、过滤条件、join 粒度、缺失值、异常值。

7. Report Generation
   生成结论、证据、限制和下一步建议。

8. Audit & Provenance
   final report 对照 trajectory、query、dataframe、chart、artifact 审计。

9. Persistence
   保存 catalog、analysis runs、artifacts、audit results、可复用 insights。
```

这条生命周期可以成为 DataAgent 的核心产品逻辑。

## 6. 哪些地方可以改

### 6.1 低风险、优先改

#### 6.1.1 扩展数据源适配器

当前 `agent/data_sources.py` 已经有 `DataSourceAdapter` 抽象，适合继续扩展。

建议新增：

```text
ExcelAdapter
ParquetAdapter
SQLiteAdapter
DuckDBAdapter
PostgresAdapter
```

优先级建议：

```text
DuckDB + Parquet + Excel + SQLite > Postgres > BigQuery/Snowflake/ClickHouse
```

原因是前几类更适合本地 DataAgent 和可控测试环境，改造风险低。

#### 6.1.2 扩展 Data Catalog

当前 catalog 只覆盖 table 和 relation。建议增加：

```text
metric definitions
business glossary
dataset lineage
analysis artifact index
column semantic types
data quality notes
source connection metadata
```

注意：连接凭据不要存在 catalog 明文里，只保存 profile-safe reference。

#### 6.1.3 新增 Analysis Artifact Store

建议新增：

```text
get_daclaw_home() / "analysis_runs"
get_daclaw_home() / "data_artifacts"
```

每次分析保存：

```text
analysis_run_id
user_task
data_sources
profile summaries
queries
python snippets
dataframe summaries
chart paths
final_report
audit_result
timestamps
model/provider metadata
```

这会让 DataAgent 具备可追溯性，也为后续 RL / eval / replay 打基础。

#### 6.1.4 强化 Analysis Audit

当前 audit 已经能审 final report。建议扩展为：

```text
report audit
query audit
join audit
metric audit
filter/time-range audit
chart audit
data quality audit
```

重点检查：

- 数值是否来自真实执行结果。
- final report 是否引用了不存在的字段。
- 是否混淆时间范围。
- join 是否改变数据粒度。
- 是否把相关性写成因果。
- 是否忽略样本量不足。
- 是否过度概括。

### 6.2 中等风险、需要设计后改

#### 6.2.1 增加 `agent_mode="data"`

当前可以用 `ephemeral_system_prompt` 临时注入 DataAgent 行为，但长期看建议显式支持：

```python
AIAgent(..., agent_mode="data")
```

Data mode 可以统一控制：

```text
默认 system prompt
默认 toolsets
是否强制 analysis audit
是否加载 data catalog context
是否保存 analysis artifacts
是否启用 data-specific compression
是否启用 stricter final answer rules
```

这样比到处用 heuristic 判断“这是不是数据分析任务”更可靠。

#### 6.2.2 独立 `data-agent` toolset

当前 data tools 已进入 core tools。未来建议区分：

```text
data              # 基础数据工具
data-agent        # 完整 DataAgent 工作流工具
data-engineering  # ETL / pipeline / warehouse 类工具
data-viz          # chart / dashboard 工具
```

完整 DataAgent toolset 可以包含：

```text
data_profile
data_catalog
data_query
data_validate
data_artifact
analysis_audit
execute_code
terminal
read_file
write_file
search_files
```

#### 6.2.3 新增 DataAgent CLI 命令

可以在 slash command registry 中增加：

```text
/data sources
/data catalog
/data profile <path>
/data mode
/data audit
/data artifacts
/data runs
```

这些命令不应该把逻辑写死在 CLI 中，而应调用 `agent/data_agent/` 的服务层。

#### 6.2.4 配置系统中增加 DataAgent 配置

建议在 config 中增加：

```yaml
data_agent:
  enabled: true
  default_engine: duckdb
  artifact_retention_days: 30
  audit:
    enabled: true
    max_repairs: 2
  profiling:
    sample_rows: 20
    max_rows: 100000
  privacy:
    redact_samples: false
```

这些配置必须通过统一配置系统读取，避免 CLI、Gateway、Web 分别读取导致语义漂移。

### 6.3 高风险、谨慎改

#### 6.3.1 不建议一开始重写 `run_agent.py`

`run_agent.py` 是当前通用 runtime 的核心，涉及：

```text
provider 适配
prompt caching
tool calling
session persistence
context compression
analysis audit
fallback / retry
trajectory
CLI display
```

DataAgent 初期应通过 mode、tool、hook、artifact store 接入，而不是直接重写主循环。

#### 6.3.2 不建议随意改 `model_tools.py` 的全局工具状态

`model_tools.py` 中存在工具解析和 `_last_resolved_tool_names` 等全局状态。它对普通 agent、subagent、RL 环境都有影响。

如需扩展 DataAgent 工具能力，优先通过 registry/toolset/schema 层扩展，不要在 dispatch 主流程里塞 DataAgent 专属分支。

#### 6.3.3 不建议破坏 Prompt Caching

DataAgent 可能需要动态 schema、catalog、memory、analysis state，但这些不应该每轮都动态改 system prompt。

更好的方式：

```text
稳定 system prompt
+ 工具查询 catalog
+ user/context block 注入必要上下文
+ artifact store 记录过程
```

## 7. DataAgent 与 RL 的结合方向

当前 RL 系统已经有很好的基础：

```text
DAClawAgentBaseEnv
DAClawAgentLoop
ToolContext
rl_training_tool
rl_cli
```

DataAgent 的 RL 不应该只训练“是否会调用工具”，而应该训练完整数据分析能力。

### 7.1 建议新增 Data Analysis Environment

可以新增：

```text
environments/data_analysis_env/
  data_analysis_env.py
  default.yaml
```

任务类型可以包括：

```text
CSV/Parquet 分析
SQL 问答
多表 join
指标计算
异常检测
cohort / retention / funnel
report faithfulness
chart interpretation
数据质量诊断
```

### 7.2 Reward 设计

DataAgent reward 可以拆成多项：

```text
schema_reward
  是否正确识别字段、类型、主键、粒度。

execution_reward
  SQL/Python 是否可以运行。

numeric_reward
  计算结果是否和 oracle 一致。

validation_reward
  是否做了 sanity check、缺失值检查、join 粒度检查。

report_reward
  final report 是否忠实、清晰、有证据。

efficiency_reward
  是否避免无意义工具调用、重复查询、过量上下文。
```

### 7.3 数据轨迹资产

为了训练 DataAgent，trajectory 不应该只存聊天文本，还应保存：

```text
tool calls
queries
query results summaries
dataframe schemas
chart metadata
audit results
reward components
final answer
```

这些数据可以同时服务：

- RL training
- SFT data generation
- failure clustering
- benchmark regression
- analysis replay

## 8. 推荐路线图

### Phase 1: 梳理边界

目标：让 DataAgent 从散落工具变成独立能力层。

建议任务：

```text
1. 新增 agent/data_agent/ 包。
2. 定义 DataAgentMode / DataAgentConfig。
3. 抽出 DataAgent prompts。
4. 定义 AnalysisRun / Artifact 数据结构。
5. 保持 run_agent.py 主循环基本不动。
```

### Phase 2: 补齐数据基础能力

目标：让 DataAgent 能处理更多真实数据源。

建议任务：

```text
1. 扩展 Excel / Parquet / SQLite / DuckDB adapter。
2. 增加 data_query 工具。
3. 增加 data_validate 工具。
4. 扩展 data_catalog 支持 metrics / glossary / lineage。
5. 所有状态写入 get_daclaw_home() 下。
```

### Phase 3: 建立可追溯分析系统

目标：让每次分析都能复盘、审计、复现。

建议任务：

```text
1. 新增 analysis_runs 存储。
2. 保存 query、代码、结果摘要、图表、final report。
3. analysis_audit 读取 structured artifacts，而不只读 conversation text。
4. CLI 增加 /data runs 和 /data artifacts。
5. Web UI 后续可以展示分析历史。
```

### Phase 4: DataAgent Mode 产品化

目标：让用户可以明确进入数据分析模式。

建议任务：

```text
1. AIAgent 支持 agent_mode="data"。
2. CLI 支持 /data mode。
3. Gateway 支持 data mode per session。
4. Data mode 默认启用 data-agent toolset。
5. Data mode 默认启用 final audit。
```

### Phase 5: DataAgent RL / Eval

目标：用评测和训练提升 DataAgent 的稳定性。

建议任务：

```text
1. 新增 environments/data_analysis_env。
2. 构建 synthetic + real data benchmark。
3. reward 拆成 schema/execution/numeric/validation/report。
4. 保存结构化 trajectory。
5. 用 audit failure 做自动数据生成和训练样本挖掘。
```

## 9. 近期可以直接处理的小问题

### 9.1 `rl_cli.py` import 顺序

`rl_cli.py` 中较早位置调用了 `get_daclaw_home()`，但 `from daclaw_constants import get_daclaw_home, OPENROUTER_BASE_URL` 出现在后面。

建议把 import 移到调用之前，避免直接运行时出现 `NameError`。

### 9.2 Data tools 是否应默认进入 core tools

当前 `_DACLAW_CORE_TOOLS` 已包含：

```text
data_profile
data_catalog
analysis_audit
```

后续需要决定：

- DAClaw 是否默认就是 DataAgent。
- 还是普通 DAClaw 保持通用，DataAgent mode 才启用完整数据工具。

如果目标是 DataAgent 驱动的 DAClaw，可以保留在 core 中；如果目标是通用 agent + data mode，则建议逐步拆出 `data-agent` toolset。

### 9.3 文档编码问题

部分文件输出中存在 emoji / 编码乱码，例如 `鉁?`、`鈿狅笍`。这不一定影响运行，但会影响 CLI 文案、文档和日志可读性。

后续如果做产品化，可以统一清理输出编码，尤其是 `rl_cli.py` 和 `environments/README.md`。

## 10. 总结

DAClaw 当前架构已经具备很强的通用 agent runtime 基础：

- 多入口接入。
- 可插拔工具系统。
- Profile 隔离。
- 会话持久化。
- Prompt caching。
- Context compression。
- Gateway。
- RL / evaluation 环境。
- 初步 DataAgent 工具和 audit 能力。

但如果 DAClaw 的未来定位是 DataAgent 驱动，那么接下来的重点不应该是继续零散增加数据工具，而应该建立 DataAgent 专属架构层。

推荐方向是：

```text
不要大拆 run_agent.py。
不要直接 fork 成另一个系统。
先建立 agent/data_agent/。
把数据源、catalog、analysis run、artifact、audit、validation、RL eval 串成闭环。
```

最终目标应该是让 DAClaw 不只是“能调用数据工具的 agent”，而是一个可以进行可追溯、可验证、可训练的数据分析 Agent 系统。

