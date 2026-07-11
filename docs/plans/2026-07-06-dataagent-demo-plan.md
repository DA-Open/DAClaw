# DAClaw DataAgent Demo — 策划文档

> 状态:**策划中(分批写入)**
> 分支:`hermes_demo`
> 作者:与用户共同策划
> 目标:在 DAClaw 通用 agent runtime 之上,做一个**非常强、实际可用**的 Data Agent demo,
> 覆盖 **Data Engineering + Data Analysis** 全流程。
>
> 本文档分批写入。每一批都是一个可讨论、可增删的模块。写完一批会停下来对齐,再写下一批。

---

## 批次进度(TOC)

- [x] **批次 1**:定位、现状盘点、Demo 目标与成功标准
- [x] **批次 2**:端到端用户故事(3~4 个 demo 剧本,决定我们到底要"演"什么)
- [x] **批次 3**:系统架构(`agent/data_agent/` 包设计 + 与 `run_agent.py` 的边界)
- [x] **批次 4**:数据源层(adapters:CSV/Excel/Parquet/SQLite/DuckDB/Postgres…)
- [x] **批次 5**:语义 Catalog + Metrics 层(schema/主键/粒度/指标口径/lineage)
- [x] **批次 6**:分析生命周期(intake→profile→plan→execute→validate→report→audit→persist)
- [x] **批次 7**:执行引擎与 Artifact 存储(SQL/Python/DuckDB 沙箱 + analysis_runs)
- [x] **批次 8**:校验与证据链(numeric/join/filter/report 审计,防幻觉)
- [x] **批次 9**:工具与 toolset 设计(data-agent toolset + 新增工具 schema)
- [x] **批次 10**:交互界面(CLI `/data` 命令 + Web WorkspacePage 呈现)
- [x] **批次 11**:实施路线图(按 demo 可演示性排优先级) + 风险
- [x] **批次 12**:Demo 脚本与验收清单(现场怎么演、演什么、如何证明"可用")
- [x] **批次 13**:重点体验线 —— 上传表格 → 美丽可视化 → 对话即时出图 → 一键生成文档(用户追加需求)

> 上面的批次划分本身也可调整——如果你觉得某块该拆开/合并/砍掉,直接说。

---

## 批次 1:定位、现状盘点、Demo 目标

### 1.1 一句话定位

> **DataAgent = 跑在 DAClaw 通用 runtime 上的一条专用"数据分析 / 数据工程" runtime(mode)。**

关键决策(沿用 `future work.md` 的判断,我认同):**不 fork、不重写 `run_agent.py`**。
DataAgent 作为一个 **mode + 专用 toolset + 服务层(`agent/data_agent/`)** 挂进现有主循环,
复用它已经很强的东西(多provider、prompt caching、context 压缩、会话持久化、子代理、审计钩子)。

这样做的好处对 demo 尤其重要:**省下的全是"演示价值低但很费时"的底盘工作**,
我们可以把精力全砸在"看得见、摸得着、能镇场"的数据能力上。

### 1.2 现状盘点(已核对代码,不是猜)

demo 不是从零开始——`hermes_demo` 分支已经有一个"能跑但很薄"的 DataAgent 雏形:

| 能力 | 现有文件 | 现在能做什么 | 明显缺口(demo 要补的) |
|---|---|---|---|
| 数据源 profiling | `agent/data_sources.py`(`DataSourceAdapter` Protocol + `CsvAdapter`)、`tools/data_analysis_tool.py`(`data_profile` 工具) | 仅**本地 CSV/TSV**:字段名、缺失值、唯一值、数值 min/max/mean、样例行 | 不支持 Excel/Parquet/SQLite/DuckDB/Postgres;profile 结果不是可复用的数据资产对象 |
| 数据目录 Catalog | `agent/data_catalog.py`、`tools/data_catalog_tool.py` | profile-scoped 持久化 **table + relation**;支持 search/get/upsert/summary(YAML 落盘在 `DACLAW_HOME/data_catalog/`) | 无 metric 定义、无 business glossary、无 lineage、无 artifact 索引、未与真实 query/run 绑定 |
| 报告审计 | `agent/analysis_audit.py`、`tools/analysis_audit_tool.py`、`run_agent.py` 的 audit hook | 识别数据分析任务→整理 trajectory→辅助 LLM 审计**最终报告**是否忠实;失败自动追加 repair prompt 让 agent 修复 | 只审最终文本;对 SQL/Python/DataFrame/chart 无结构化追踪;无法证明数值来自可复现执行 |
| 执行 | `tools/code_execution_tool.py`、`tools/terminal_tool.py`(6 种后端:local/docker/ssh/daytona/modal/singularity) | 能跑 Python/shell,有沙箱隔离 | 没有"数据分析专用"的执行封装(结果摘要、artifact 落盘、可复现) |
| 核心工具集 | `toolsets.py` 的 `_DACLAW_CORE_TOOLS` | `data_profile`/`data_catalog`/`analysis_audit` 已默认进核心工具 | 没有完整 `data-agent` toolset(query/validate/artifact 等未成形) |

**一句话总结现状**:现在是"通用 agent + 3 个数据工具 + 最终报告审计",
还**不是**"面向数据任务全生命周期、有 artifact/证据链/可复现的 DataAgent"。demo 要跨越的正是这道坎。

### 1.3 Demo 目标(我们到底要证明什么)

这个 demo 要能当场证明三件事——我把它们定为**北极星**,后续所有批次都对齐它们:

1. **覆盖全流程**:从"给我一份 CSV / 一个数据库"到"产出一份带证据链的分析报告 + 可复现的 artifact",
   Data Engineering(接入、profiling、建模、清洗、入仓)和 Data Analysis(探索、指标、可视化、结论)两段都要走通。
2. **可信,不是"看起来对"**:报告里的每个数字都能溯源到一次真实执行(query/代码);
   有校验层拦截常见数据谎言(join 改变粒度、时间范围错配、相关当因果、样本量不足、引用不存在的字段)。
3. **实际可用**:不是 mock。真连数据源、真跑 SQL/DuckDB、真存 artifact、真在 CLI 和 Web 里能操作。
   跑在 $5 VPS 上也能用(本地 DuckDB/SQLite 优先,不强依赖云仓)。

### 1.4 明确的非目标(demo 阶段先不做,避免范围爆炸)

- **不**做企业级权限/多租户治理(只做 profile 级隔离,复用现有 `get_daclaw_home()`)。
- **不**首发就接 BigQuery/Snowflake/ClickHouse(优先本地可控源:DuckDB+Parquet+Excel+SQLite;Postgres 次之)。
- **不**重写主循环 `run_agent.py`(通过 mode/tool/hook/artifact 挂入)。
- **不**做完整 RL 训练闭环(demo 里最多预留 trajectory/artifact 的结构,为将来铺路)。

### 1.5 成功标准(可验收)

demo 达成即满足以下**可勾选**清单(细化版放批次 12):

- [ ] 能接入 ≥4 类数据源(CSV、Excel、Parquet、SQLite/DuckDB)并生成结构化 profile。
- [ ] 能把 schema/主键/粒度/指标口径写进语义 catalog 并跨会话复用。
- [ ] 能对一个"多表 join + 指标计算"的真实问题,自动规划→执行→校验→出报告。
- [ ] 报告中每个关键数字可点开看到**产出它的 query/代码 + 结果摘要**(证据链)。
- [ ] 校验层能在一个**故意埋雷**的数据集上抓出至少 3 类错误(如 join 膨胀、缺失值、时间错配)。
- [ ] 每次分析落一个 `analysis_run`,可 `/data runs` 列出、`/data artifacts` 查看、可复现。
- [ ] CLI 与 Web 两个入口都能驱动同一条 DataAgent 流程(不重复实现)。

---

> **批次 1 完成。** 下面停下来对齐:
>
> 1. 上面的**定位、非目标、成功标准**是否符合你心里的 demo?有没有要加/砍的?
> 2. 数据源优先级(DuckDB+Parquet+Excel+SQLite 优先、Postgres 次之、云仓暂缓)你认可吗?还是 demo 必须现场连某个特定的库/仓?
> 3. 你的 demo **受众和场景**是什么?(内部技术评审 / 对外客户演示 / 招聘展示 / 自用工具)—— 这会直接影响批次 2 的"用户故事"该往哪个方向编。
>
> 你回一下,我就接着写**批次 2:端到端用户故事**。

---

## 批次 2:端到端用户故事(我们到底要"演"什么)

> **两个待你确认项的当前假设**(标注为可推翻,先不阻塞进度):
> - **受众/场景**:假设为「内部技术评审为主 + 可平滑转对外客户演示」。所以剧本既要展示技术深度(证据链、校验),又要有"业务人也能看懂"的结论层。
> - **数据源优先级**:DuckDB+Parquet+Excel+SQLite 优先,Postgres 次之,云仓暂缓。
> - 如果这两条要改,剧本 A/B/C 的数据源和话术要跟着调——你说一声即可。

### 2.0 一个前置事实:仓库里没有样例数据(0 个)

已核查:`hermes_demo` 里没有任何 `.csv/.parquet/.sqlite/.db`。
所以 demo **必须自带一个可复现的数据集生成器**(seed 固定、一条命令生成),这有三个好处:

1. **可复现**:任何人 clone 下来 `daclaw data demo-seed` 就能得到同一份数据,演示不翻车。
2. **可埋雷**:我们故意在数据里埋脏数据/陷阱(见剧本 C),用来证明"校验层真的能抓错"。
3. **无隐私风险**:全合成,可对外演示。

拟生成一个**小型电商数据集**(约 5 张表,量级几万行,本地 DuckDB/Parquet/CSV/SQLite 各出一份):
`customers`、`orders`、`order_items`、`products`、`events`(埋点)。
—— 这套 schema 足够演"多表 join + 指标 + 时间序列 + 数据质量"全部戏份,又小到能塞进 $5 VPS。

> 备选:如果你更想贴合**你实际的业务领域**(比如金融交易、广告投放、SaaS 订阅、物流),
> 告诉我领域,我把这套合成 schema 换成对应领域的。电商是"最大公约数",默认用它。

### 2.1 剧本 A —— "冷启动接入 + 建模"(Data Engineering 主场)

**角色**:数据工程师,刚拿到一个陌生的 SQLite 库,什么都不知道。

**一句话 prompt**:
> "这是 `shop.sqlite`,帮我搞清楚里面有什么、表之间怎么关联,建好数据目录。"

**Agent 应当自动走完**:
1. `data_source_connect` 连上 SQLite → 列出所有表。
2. 对每张表 `data_profile`:字段、类型、缺失率、唯一值、数值分布、样例行(复用现有 `column_stats` 结构,扩展到 DB 表)。
3. **推断主键与外键关系**(唯一性 + 命名 + 值域重叠),写进 `data_catalog` 的 table/relation。
4. 产出一张 **schema/ER 概览**(文本 + 简单图),并把"我推断出的粒度和关系"讲给用户听。

**演示爽点**:从"一个黑盒 .sqlite"到"一份带主外键关系的语义目录",全自动,且**目录跨会话留存**——下次直接用。

**证明"可用"**:`/data catalog` 能列出刚建好的表和关系;关掉重开,catalog 还在。

### 2.2 剧本 B —— "一句话业务问题 → 带证据链的报告"(Data Analysis 主场)

**角色**:业务/运营,不写 SQL。

**一句话 prompt**:
> "过去 90 天,哪些商品品类的复购率最高?按月看有没有趋势?"

**Agent 应当自动走完**(这是核心生命周期,批次 6 详述):
1. **Plan**:读 catalog,判断需要 `orders`×`order_items`×`products` 三表 join,定义"复购率"口径(≥2 单的客户占比),锁定 90 天时间窗。
2. **Execute**:生成并**真实执行** SQL/DuckDB(不是假装),得到结果表。
3. **Validate**:自动校验——join 后行数是否异常膨胀?时间窗过滤对不对?样本量够不够?(批次 8)
4. **Report**:给出结论 + 每月趋势(小图表)+ **明确的口径说明和局限**。
5. **Persist**:落一个 `analysis_run`,保存 query、结果摘要、图、报告。

**演示爽点(镇场关键)**:报告里每个数字旁边有个"来源"——**点开就是产出它的那条 SQL + 结果摘要**。
这就是与"ChatGPT 编个看起来对的数"最本质的区别:**可溯源、可复现**。

**证明"可用"**:`/data runs` 看到这次分析;`/data artifacts <run_id>` 能取出 SQL 和结果;重跑得到一致数字。

### 2.3 剧本 C —— "抓谎"(可信度主场,最有说服力的一幕)

**角色**:审阅者,故意刁难。

**背景**:数据集里我们**故意埋雷**——
- `order_items` 里有重复行(会让 join 后金额虚高);
- 某月 `orders` 有一批 `NULL` 金额;
- `events` 表时间戳跨了时区(会让"按天"统计错位)。

**一句话 prompt**:
> "算一下这些订单的总 GMV,以及每天的下单量。"

**Agent 应当**:
1. 先按常规算出一个数;
2. **校验层介入**并主动报告:"检测到 `order_items` 存在重复行,直接 SUM 会让 GMV 虚高 X%,我用去重后的口径重算";"某月 12% 订单金额为 NULL,已排除并在报告中标注";"`events` 时间戳含多时区,已统一到 UTC 再按天聚合"。
3. 给出**修正后的数字** + **一段'我发现并规避了哪些坑'的说明**。

**演示爽点**:大多数"AI 数据分析"demo 会安静地算错。**我们的 demo 会当场抓出自己/数据的问题并纠正**——这是"实际可用"最硬的证据。

**证明"可用"**:同一份埋雷数据,校验层每次都稳定抓出这 3 类问题(可写进验收清单)。

### 2.4 剧本 D(可选,加分项)—— "数据工程流水线 + 定时"

**角色**:想把上面的分析变成每天自动跑。

**一句话 prompt**:
> "把'每日 GMV 和新增客户'做成每天早上 9 点自动生成的报告,发我 Telegram。"

**Agent 应当**:复用现有 `cron/` 调度 + `gateway/` 消息投递,把剧本 B 的分析封装成一个**定时数据管道**,
产物同样落 `analysis_run`,并通过消息平台推送摘要。

**演示爽点**:展示 DataAgent 不只是"问一句答一句",而是能**沉淀成无人值守的数据管道**——
这一步几乎白捡,因为 cron+gateway 是 DAClaw 现成能力,只需把分析封装进去。

### 2.5 四个剧本如何映射到"全覆盖"

| 能力域 | 剧本 A | 剧本 B | 剧本 C | 剧本 D |
|---|:--:|:--:|:--:|:--:|
| 数据源接入(DE) | ✅ 主 | ✅ | ✅ | ✅ |
| Profiling / 建模(DE) | ✅ 主 | | | |
| 语义 Catalog / 指标口径 | ✅ | ✅ 主 | | ✅ |
| 分析规划 + 执行(DA) | | ✅ 主 | ✅ | ✅ |
| 校验 / 证据链 / 防幻觉 | | ✅ | ✅ 主 | |
| Artifact / 可复现 | ✅ | ✅ | ✅ | ✅ 主 |
| 可视化 / 报告 | | ✅ | ✅ | ✅ |
| 数据管道 / 调度(DE) | | | | ✅ 主 |

**A+B+C 三个必演**,就已覆盖 Data Engineering 与 Data Analysis 的主干;**D 是加分**,用现成能力串起来。

---

> **批次 2 完成。** 停下来对齐:
>
> 1. **合成电商数据集**(customers/orders/order_items/products/events)可以吗?还是换成**你的实际业务领域**(金融/广告/SaaS/物流…)?这决定我下一步写生成器时用什么 schema。
> 2. **四个剧本**——A(接入建模)、B(问题→报告)、C(抓谎)、D(定时管道)——哪些是**必演**?我默认 A+B+C 必演、D 加分。你想砍哪个或加哪个?
> 3. 剧本 C 的**"抓谎"**是我认为最能镇场的一幕(也是技术含量最高的校验层)。你认同把它作为 demo 的**高潮/卖点**吗?还是你更看重别的(比如接入的数据源种类多、或报告的可视化好看)?
>
> 你定完,我接着写**批次 3:系统架构**(`agent/data_agent/` 包怎么设计、怎么挂进 `run_agent.py` 而不动主循环)。

---

## 批次 3:系统架构(挂载,而非重写)

> 全部对齐过真实代码:`AIAgent.__init__` 已有 `enabled_toolsets` / `disabled_toolsets` /
> `ephemeral_system_prompt` / `max_iterations` 参数(`run_agent.py:566+`);
> 审计钩子已存在 `_should_run_analysis_audit` / `audit_analysis_report` / `looks_like_analysis_task`
> (`run_agent.py:3517`、`agent/analysis_audit.py`)。我们**接这些现成挂点**,不新造主循环。

### 3.1 设计原则

1. **单一大脑**:仍然只有 `AIAgent.run_conversation()` 一个循环。DataAgent 不是第二个 runtime。
2. **服务层下沉**:所有数据逻辑放进新包 `agent/data_agent/`,由工具层调用;CLI/Web/Gateway 都调**同一个服务层**,杜绝三处各写一遍。
3. **Mode 驱动**:用一个 `DataAgentMode` 决定"这一轮启用哪些 toolset、注入什么 prompt、是否强制审计、是否落 artifact"。demo 阶段先用现成的 `ephemeral_system_prompt` + `enabled_toolsets` 组合实现,**零侵入**;成熟后再考虑给 `AIAgent` 加显式 `agent_mode="data"` 参数(批次 11 路线图)。
4. **状态跟 profile 走**:所有落盘用 `get_daclaw_home()`(已是全仓约定),天然多 profile 隔离。

### 3.2 新增包结构 `agent/data_agent/`

```text
agent/data_agent/
  __init__.py
  mode.py          # DataAgentMode / DataAgentConfig:决定 toolset、prompt、audit、artifact 开关
  prompts.py       # DataAgent 专用 system/developer prompt(强调:先建模、口径先行、数字必溯源)
  workflow.py      # 分析生命周期编排(批次 6):intake→profile→plan→execute→validate→report→persist
  sources.py       # 数据源连接与适配注册中心(批次 4);复用/扩展 agent/data_sources.py
  catalog.py       # 语义 catalog 封装(批次 5);复用/扩展 agent/data_catalog.py
  metrics.py       # 指标定义与口径管理(批次 5)
  artifacts.py     # analysis_run / artifact 存储(批次 7)
  validators.py    # 数值/join/filter/schema/report 校验(批次 8)
  provenance.py    # 证据链:数字 → 产出它的 query/代码 的绑定(批次 8)
```

**关键边界**:这个包**不 import `run_agent`**(避免循环依赖),它是被 `tools/*.py` 调用的纯服务层。
`run_agent.py` 只通过"工具调用"和"已有 audit 钩子"间接触达它。

### 3.3 数据流(在现有主循环里的位置)

```text
用户问题
  ↓
AIAgent.run_conversation()                       ← 不改
  ↓  (DataAgentMode 提供 ephemeral_system_prompt + enabled_toolsets=["data-agent",...])
模型发起 tool_call
  ↓
model_tools.handle_function_call()               ← 不改
  ↓
tools/registry.dispatch()                        ← 不改
  ↓
tools/data_*.py  ──调用──▶  agent/data_agent/ 服务层   ← 新增都在这条线右侧
  ↓
返回 JSON(含 artifact 引用)→ 回填 → 下一轮
  ↓
(回合末) _should_run_analysis_audit 命中 → audit_analysis_report()   ← 已存在,我们喂给它结构化 artifact
  ↓
审计失败 → 自动 repair prompt(已存在机制)→ agent 修复后再返回
```

**这张图的意义**:整条竖线(主循环、分发、审计、修复)**全是现成的**;我们所有新增(右侧)都挂在工具与服务层。风险被压在最小面。

### 3.4 落盘布局(profile-scoped)

```text
$DACLAW_HOME/
  data_catalog/           # 已存在(table/relation),批次 5 扩展 metrics/glossary/lineage
    <source-slug>/
      tables/*.yaml
      relations/*.yaml
      metrics/*.yaml       # 新增
  analysis_runs/          # 新增(批次 7):每次分析一条,含 query/代码/结果摘要/图/报告/审计
    <run_id>/
      run.json
      queries/*.sql
      code/*.py
      results/*.parquet|*.json
      charts/*.png
      report.md
      audit.json
  data_sources/           # 新增:连接元数据(不存明文凭据,只存 profile-safe reference)
    <source-slug>.yaml
```

---

## 批次 4:数据源层(adapters)

> 现状:`agent/data_sources.py` 已有 `DataSourceAdapter` Protocol + `CsvAdapter`,
> `profile()` 返回 `{adapter, source, rows_profiled, truncated, columns, column_stats, sample_rows}`
> (已核对 `agent/data_sources.py:159-215`)。`_ADAPTERS = [CsvAdapter()]`。
> 我们**沿用这个 Protocol**,只加新 adapter + 让 adapter 支持"多表 + 可执行查询"。

### 4.1 要扩展的能力

现有 `DataSourceAdapter` 只有 `profile(source)`。DataAgent 需要 adapter 再提供:

| 方法 | 用途 | 备注 |
|---|---|---|
| `list_tables()` | DB 类源列出所有表 | 文件类源返回单"表" |
| `profile(table=None)` | 单表/单文件 profiling | 复用现有输出结构,新增 `table` 维度 |
| `run_query(sql) -> ResultTable` | **执行**查询 | 文件类源经 DuckDB 挂载后也能 SQL(见 4.3) |
| `sample(table, n)` | 取样 | profiling 与预览共用 |
| `capabilities()` | 声明支持 SQL/关系/事务等 | 让 planner 知道能不能下推 SQL |

### 4.2 adapter 优先级(默认,可推翻)

| 优先级 | Adapter | 依赖 | 为什么 |
|---|---|---|---|
| **P0(demo 必备)** | `CsvAdapter`(已有)、`SqliteAdapter`、`DuckDBAdapter`、`ParquetAdapter`、`ExcelAdapter` | stdlib `sqlite3`、`duckdb`、`pyarrow`/`pandas`、`openpyxl` | 本地、可控、$5 VPS 能跑、覆盖剧本 A/B/C |
| **P1** | `PostgresAdapter` | `psycopg`(可选依赖) | 演"连真实数据库",但非必需 |
| **P2(暂缓)** | BigQuery / Snowflake / ClickHouse | 各自 SDK | 云仓,demo 阶段不做 |

依赖策略:P0 里除 `duckdb`/`pyarrow`/`openpyxl` 外都是 stdlib;这几个作为**可选 extra**(`.[data]`),
未安装时 adapter 优雅缺席(仿照现有 `check_data_requirements()` 模式),不拖垮主程序。

### 4.3 关键设计:DuckDB 作为统一执行层

**一个杠杆点**:DuckDB 能直接 `SELECT * FROM 'file.parquet'` / `read_csv_auto()` / `ATTACH` SQLite。
所以 **CSV/Parquet/Excel/SQLite 都可以通过 DuckDB 获得 SQL 执行能力**,不必给每种格式各写一套查询引擎。

```text
文件源(csv/parquet/xlsx) ─┐
SQLite 源 ────────────────┼─▶ DuckDB 内存/本地库(ATTACH / read_*)─▶ 统一 run_query(sql)
DuckDB 源 ────────────────┘
Postgres(P1)────────────────▶ 原生 psycopg 执行(或 DuckDB postgres_scanner)
```

这让批次 6/7 的"执行"部分**只需面向一个 SQL 接口**,大幅降低实现量——对 demo 是决定性的省力点。

### 4.4 连接元数据与安全

- 连接信息落 `data_sources/<slug>.yaml`,**只存 profile-safe reference**(host/db 名/文件路径),
  **不存明文密码**;凭据走现有 `tools/credential_files.py` / 环境变量(复用现成机制)。
- 文件路径经现有 `tools/path_security.py` 校验,防越权读取。

---

## 批次 5:语义 Catalog + Metrics 层

> 现状:`agent/data_catalog.py` 已持久化 table + relation(YAML,profile-scoped),
> 有 `normalize_table_spec` / `normalize_relation_spec` / search / get / upsert / summary
> (已核对 `agent/data_catalog.py`)。我们**在其上加三层语义**,不改现有存储格式。

### 5.1 为什么 catalog 是 DataAgent 的"长期记忆"

普通 memory 存的是"用户偏好、事实"。**数据语义不能混进普通 memory**——它需要结构化、可查询、可校验。
catalog 就是 DataAgent 的领域记忆:一次建模,多次复用;跨会话不丢;是 planner 生成正确 SQL 的**事实来源**。

### 5.2 现有 table/relation 记录要补的字段

现有 table 记录有字段/类型/主键雏形。demo 要补齐(让 planner 能正确 join、正确聚合):

```yaml
# tables/orders.yaml(扩展后)
kind: table
source: shop.sqlite
table: orders
columns:
  - name: order_id
    type: integer
    semantic_type: identifier      # 新增:identifier/measure/dimension/timestamp/category
    nullable: false
  - name: customer_id
    type: integer
    semantic_type: foreign_key     # 新增
  - name: amount
    type: numeric
    semantic_type: measure         # 新增:可被 SUM/AVG
    unit: CNY                       # 新增:口径单位
  - name: created_at
    type: timestamp
    semantic_type: timestamp
    timezone: UTC                   # 新增:防时区错配(剧本 C)
primary_key: [order_id]            # 已有雏形
grain: "one row per order"         # 新增:粒度声明(防 join 膨胀的关键)
row_count: 48213                    # 新增:profiling 时回填
quality_notes:                      # 新增
  - "2026-03 有 12% amount 为 NULL"
```

### 5.3 新增:Metrics(指标口径)—— 防"同名不同义"

`metrics/*.yaml`,把业务指标的**口径固化**,让"复购率""GMV"这类词有唯一定义:

```yaml
# metrics/repurchase_rate.yaml
name: repurchase_rate
label: 复购率
description: 在给定时间窗内下单 ≥2 次的客户 占 有下单客户 的比例
grain: customer
depends_on_tables: [orders]
definition_sql: |
  SELECT COUNT(*) FILTER (WHERE order_cnt >= 2)::FLOAT
         / NULLIF(COUNT(*), 0)
  FROM (SELECT customer_id, COUNT(*) order_cnt
        FROM orders
        WHERE created_at >= :window_start AND created_at < :window_end
        GROUP BY 1)
default_window_days: 90
caveats:
  - "退款订单是否计入需确认;当前口径含退款"
```

**demo 价值**:剧本 B 里,agent 不是"临时编一个复购率算法",而是**引用 catalog 里定稿的口径**——
这就是"可信"的另一半(除了数字可溯源,口径也可溯源)。

### 5.4 新增:轻量 Lineage / Glossary(按需,加分)

- **Lineage**:`analysis_run` 产出时记录"用了哪些表/指标",反向可查"这个表被哪些分析用过"。demo 里做**最小可用**版本即可(存 run→tables 的引用)。
- **Glossary**:业务术语→表/字段/指标的映射(如"活跃用户"= `events` 表 30 天内有行为的 `customer_id`)。demo 里作为可选,几条示例足够体现能力。

### 5.5 catalog 如何进入 prompt(不破坏 prompt caching)

**重要约束**(来自 `future work.md` 6.3.3,我认同):**不要每轮把整个 catalog 塞进 system prompt**——
那会让 Anthropic prefix cache 每轮失效,又贵又慢。正确做法:

```text
稳定的 DataAgent system prompt(只说"你可以用 data_catalog 工具查询 schema")
  + agent 按需调用 catalog 工具拿具体表结构
  + 必要上下文以 user/context block 注入(现有 memory 注入机制已这么做)
```

即:**catalog 是"工具可查的数据库",不是"prompt 里的常驻文本"**。这条对 demo 的响应速度和成本都关键。

---

> **批次 3~5 完成。** 继续写批次 6~8(生命周期、执行引擎、校验)。

---

## 批次 6:分析生命周期(核心编排)

> 这是 DataAgent 的**产品逻辑主干**,由 `agent/data_agent/workflow.py` 编排。
> 注意:它**不是**一个脱离主循环的状态机——而是一组"agent 该按顺序调用的工具 + 每步的 guardrail"。
> agent 仍在 `run_conversation()` 里自由决策,workflow 提供的是**推荐路径 + 关键卡点**。

### 6.1 九个阶段

```text
1. Intake        接收问题 + 数据源 + 业务背景 + 期望输出
2. Profile       识别源 → profiling(复用批次 4)
3. Catalog       写/读语义目录:schema、主键、粒度、指标口径(批次 5)
4. Plan          生成分析计划:用哪些表、怎么 join、怎么过滤、算哪些指标
5. Execute       真实执行 SQL/Python(批次 7),得到结果 + artifact
6. Validate      校验:numeric/join/filter/时间/样本(批次 8)—— 不通过则回到 4/5
7. Report        结论 + 证据 + 局限 + 下一步
8. Audit         已有 audit hook:report 对照 trajectory/artifact(批次 8)
9. Persist       落 analysis_run(批次 7)
```

### 6.2 每阶段的输入/输出/卡点

| 阶段 | 输入 | 输出 | 卡点(不满足就不许往下走) |
|---|---|---|---|
| Intake | 用户 prompt | 结构化任务:目标指标、时间窗、维度、数据源 | 目标或数据源缺失 → 用 `clarify` 工具反问(现有工具) |
| Profile | 数据源 | profile 对象 | 源连不上/空表 → 报错并停 |
| Catalog | profile | table/relation/metric 记录 | 若指标口径未定义 → 先定义再算 |
| Plan | catalog + 任务 | 分析计划(表/join/过滤/指标/步骤) | join 未声明粒度 → 拒绝(防膨胀) |
| Execute | 计划 | 结果表 + query/code artifact | 执行报错 → 带错误重试(现有 retry 机制) |
| Validate | 结果 + 计划 | 校验报告 | 校验红灯 → 回 Plan/Execute 修正 |
| Report | 结果 + 校验 | 报告(含证据链引用) | 数字无 artifact 来源 → 拒绝出报告 |
| Audit | 报告 + artifact | audit 结论 | 审计失败 → 现有 repair prompt 机制自动修 |
| Persist | 全部 | analysis_run 目录 | — |

### 6.3 关键设计:计划(Plan)是一个可校验的对象,不是一段自然语言

Plan 阶段产出一个**结构化计划**(存进 artifact),而不是让模型"心里想一下就直接写 SQL":

```json
{
  "question": "过去90天各品类复购率及月度趋势",
  "time_window": {"start": "2026-04-07", "end": "2026-07-06", "column": "orders.created_at", "tz": "UTC"},
  "tables": ["orders", "order_items", "products"],
  "joins": [
    {"left": "order_items.order_id", "right": "orders.order_id", "grain_check": "orders 是 1:N 到 order_items,聚合前需按 order 去重"},
    {"left": "order_items.product_id", "right": "products.product_id", "grain_check": "products 是维表,不改变粒度"}
  ],
  "metrics": ["repurchase_rate"],
  "dimensions": ["products.category", "date_trunc('month', orders.created_at)"],
  "steps": ["按月按品类聚合下单客户", "计算复购客户占比", "出趋势"]
}
```

**为什么这么设计**:有了结构化计划,**校验层(批次 8)就能在执行前先审计计划本身**——
比如"这个 join 会不会改变粒度""时间列选对了没""指标口径匹配吗"。
这是把"事后发现算错"变成"事前拦截"的关键一步,也是 demo 里"抓谎"能稳定复现的基础。

### 6.4 demo 阶段的务实简化

- 生命周期**不强制全部九步**:简单问题(单表计数)可跳过 Plan 的复杂校验。用 `looks_like_analysis_task`(已存在)判断复杂度。
- Plan 由 LLM 生成、但**结构受 schema 约束**(用 structured output / 工具参数校验),不是自由文本。
- Validate 与 Audit 有重叠:**Validate 是执行期硬校验(规则+数值)**,**Audit 是回合末 LLM 复审(现有机制)**。两者互补,批次 8 讲清分工。

---

## 批次 7:执行引擎与 Artifact 存储

> 现状:已有 `tools/code_execution_tool.py` + `tools/terminal_tool.py`(6 种后端)。
> 我们**不新造沙箱**,而是在其上加一层"数据分析专用执行封装",让每次执行都产出可复现 artifact。

### 7.1 执行引擎:一个 SQL 接口 + 受控 Python

承接批次 4.3 的杠杆:**DuckDB 是统一 SQL 执行层**。执行工具分两类:

| 工具 | 干什么 | 底层 | artifact |
|---|---|---|---|
| `data_query` | 跑 SQL(对任意已接入源,经 DuckDB) | DuckDB / psycopg | `.sql` + 结果 `.parquet` + 结果摘要 |
| `data_execute_code` | 跑分析 Python(pandas/duckdb/matplotlib) | 现有 code_execution 沙箱 | `.py` + stdout + 生成的 `.png`/`.parquet` |

**约束(防幻觉的地基)**:报告里的数字**只能来自 `data_query`/`data_execute_code` 的真实返回**,
不允许模型"心算"或"估计"。每个结果都带一个 `artifact_id`,报告引用它。

### 7.2 ResultTable:执行结果的标准信封

所有执行返回统一结构(存 artifact + 喂回模型时是**摘要**,不是全量,省 context):

```json
{
  "artifact_id": "res_a1b2",
  "kind": "sql_result",
  "query_ref": "queries/q_003.sql",
  "columns": [{"name": "category", "type": "text"}, {"name": "repurchase_rate", "type": "double"}],
  "row_count": 8,
  "sample_rows": [["数码", 0.34], ["服饰", 0.29]],
  "full_result_path": "results/res_a1b2.parquet",
  "stats": {"null_counts": {...}, "elapsed_ms": 42},
  "truncated_preview": true
}
```

**要点**:回填给模型的是 `sample_rows` + `row_count` + 统计(几百 token),
**全量结果落在 parquet**。这既保证数字真实,又不让大结果撑爆 context——对 demo 的响应速度关键。

### 7.3 Artifact 存储:analysis_runs

每次分析一个 run 目录(批次 3.4 已给布局),`agent/data_agent/artifacts.py` 负责读写:

```python
# 伪代码,示意 API 形状
run = ArtifactStore.new_run(user_task=..., data_sources=[...])
run.save_plan(plan_dict)
qid = run.save_query(sql, result_table)          # 返回可被报告引用的 id
cid = run.save_code(py_src, stdout, charts=[...])
run.save_report(markdown, references=[qid, cid]) # 报告显式声明引用了哪些 artifact
run.save_audit(audit_result)
run.finalize()   # 写 run.json,标记完成
```

**demo 价值**:`/data runs` 列出所有分析,`/data artifacts <run_id>` 展开看到 SQL/代码/结果/图/报告全链路,
**且能一键重跑**(`data_query` 用存下的 SQL 重放,验证数字一致)——这就是"可复现"的实证。

### 7.4 与现有 trajectory 的关系

DAClaw 已有 `trajectory.py`(为 RL/训练存对话轨迹)。analysis_run 是它的**数据分析特化补充**:
trajectory 存"对话发生了什么",analysis_run 存"分析产出了什么(可复现的数据资产)"。
demo 阶段两者独立即可;将来做 DataAgent RL 时,analysis_run 正好是结构化训练信号(见批次 11)。

---

## 批次 8:校验与证据链(demo 的"可信"内核 + 抓谎高潮)

> 分工:**Validators = 执行期硬校验(规则/数值,确定性)**;
> **Audit = 回合末 LLM 复审(语义忠实性,已有机制 `audit_analysis_report`)**。
> 两者叠加 = 既防"机械错误"又防"叙事失真"。

### 8.1 Validators(`agent/data_agent/validators.py`)—— 确定性规则

在 Execute 前后自动运行,**不依赖 LLM**(所以稳定、可复现,适合写进验收):

| 校验器 | 检查什么 | 抓的雷(对应剧本 C) |
|---|---|---|
| `join_grain_validator` | join 后行数 vs 预期粒度;是否出现意外的一对多放大 | `order_items` 重复行导致 GMV 虚高 |
| `null_validator` | 参与聚合的列缺失率;是否静默丢行 | 某月 12% `amount` 为 NULL |
| `time_window_validator` | 时间列时区一致性、窗口边界、是否跨时区聚合 | `events` 多时区导致按天错位 |
| `sample_size_validator` | 分组后每组样本量;小样本结论标注 | 某品类只有 3 单却下强结论 |
| `numeric_sanity_validator` | 比率∈[0,1]、非负金额、求和守恒(明细 SUM == 汇总) | 占比 >100%、金额为负 |
| `schema_ref_validator` | 报告/SQL 引用的字段是否真实存在于 catalog | 引用了不存在的列 |

**每个校验器输出**:`{level: pass|warn|fail, message, evidence}`。
`fail` → workflow 回到 Plan/Execute 修正;`warn` → 允许继续但**必须写进报告的"局限"**。

### 8.2 证据链(`agent/data_agent/provenance.py`)—— 数字 ↔ 产出它的执行

**核心不变式**:报告里每一个具体数值,都必须绑定一个 `artifact_id`(来自 7.2 的 ResultTable)。

```text
报告文本:"数码品类复购率 34%[^res_a1b2]"
                                  │
                                  ▼
          provenance 索引:res_a1b2 → queries/q_003.sql + results/res_a1b2.parquet
```

实现方式(demo 够用):
- Report 阶段要求模型**以引用形式**写数字(工具强制:出报告的工具参数里,`claims` 是 `[{text, value, artifact_id}]` 列表)。
- `provenance` 校验:报告里出现的数字若**没有** artifact 绑定 → 出报告工具直接拒绝,退回让模型用 `data_query` 得到真实来源。

**这就是与"看起来对的 AI"的分水岭**:不是"我相信模型算对了",而是"每个数字都能点开看到它从哪条 query 来的"。

### 8.3 Audit(复用现有 `audit_analysis_report`)—— 语义忠实性复审

现有机制审的是"报告是否忠实于分析过程"。我们**升级它的输入**:
从"只喂对话文本" → "喂结构化 artifact(plan + queries + result summaries + validator 报告)"。
这样 LLM 审计员能问更犀利的问题:

- 报告结论和 result 表的数字一致吗?
- 有没有把 `warn` 级的局限藏起来不说?
- 有没有把相关性写成因果?
- 有没有超出数据范围过度概括?

审计失败 → 现有 repair prompt 机制自动触发,让 agent 修完再返回用户(**用户看不到这个内部过程**,只看到更靠谱的最终答案)。

### 8.4 剧本 C(抓谎)如何靠这套稳定复现

埋雷数据 + 上面的确定性 validators = **每次都能抓到同样的问题**(不靠 LLM 运气):

```text
用户:"算总 GMV 和每日下单量"
  → Execute 初版 SQL
  → join_grain_validator: FAIL「order_items 存在 1,204 条重复行,SUM 前需去重,否则 GMV 虚高 ~7.8%」
  → null_validator:      WARN「2026-03 有 12% amount 为 NULL,已排除,报告需标注」
  → time_window_validator: FAIL「events.ts 含 +08:00 与 UTC 混合,按天聚合前统一到 UTC」
  → workflow 自动回到 Execute,修正 SQL(去重 + 时区归一 + NULL 处理)
  → 重新校验通过
  → 报告:修正后的 GMV + 每日下单量 + 「我发现并规避了 3 类数据质量问题」说明
```

**验收点**:同一份埋雷数据,跑 N 次,这 3 类问题**每次都被抓出**(确定性校验器保证)。这是 demo 最硬的"可用"证据。

---

> **批次 6~8 完成。** 继续写批次 9~10(工具/toolset、CLI+Web 界面)。

---

## 批次 9:工具与 toolset 设计

> 全部对齐真实注册 API:`registry.register(name, toolset, schema, handler, check_fn=, emoji=, max_result_size_chars=)`
> (已核对 `tools/registry.py:176` 与 `tools/data_analysis_tool.py:91`)。
> 新工具**照抄现有 `data_profile` 的注册结构**,零学习成本。

### 9.1 新增/扩展的工具清单

| 工具名 | 状态 | 输入(schema 要点) | 输出 | 归属 toolset |
|---|---|---|---|---|
| `data_source_connect` | 新增 | `{source, kind?}`(路径或连接 ref) | 连接确认 + 表列表 | `data-agent` |
| `data_profile` | **扩展**(已有) | 加 `table?` 维度、支持 DB/Parquet/Excel | 现有结构 + `table`/`row_count` | `data` / `data-agent` |
| `data_catalog` | **扩展**(已有) | 加 metric upsert、grain/semantic_type 字段 | 现有 + metric 记录 | `data` / `data-agent` |
| `data_query` | 新增 | `{sql, source?}` | ResultTable 信封(批次 7.2) | `data-agent` |
| `data_validate` | 新增 | `{run_id?, checks?}` | 各 validator 的 pass/warn/fail(批次 8.1) | `data-agent` |
| `data_execute_code` | 新增(封装现有 execute_code) | `{code, run_id}` | code artifact + 图/结果 | `data-agent` |
| `data_report` | 新增 | `{claims:[{text,value,artifact_id}], narrative, caveats}` | 报告 + 强制证据链校验(批次 8.2) | `data-agent` |
| `analysis_audit` | 已有 | — | — | `data` |

> 说明:`data_profile`/`data_catalog`/`analysis_audit` 已在 `_DACLAW_CORE_TOOLS` 里(核对过),
> 保持不动;新增的 4 个进 `data-agent` toolset,**默认不进 core**(见 9.3 的开关策略)。

### 9.2 注册代码形状(以 `data_query` 为例,可直接落地)

```python
# tools/data_query_tool.py
from tools import registry
from agent.data_agent.workflow import run_query          # 服务层在 agent/data_agent/
from agent.data_agent.artifacts import current_run

DATA_QUERY_SCHEMA = {
    "name": "data_query",
    "description": (
        "Execute a read-only SQL query against a connected data source and "
        "return a result summary. Every number in your final report MUST come "
        "from a data_query or data_execute_code result — never estimate."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "Read-only SELECT query"},
            "source": {"type": "string", "description": "Source slug; defaults to active source"},
        },
        "required": ["sql"],
    },
}

def _handle_data_query(args, **kwargs):
    return run_query(sql=args["sql"], source=args.get("source"), run=current_run())

registry.register(
    name="data_query",
    toolset="data-agent",
    schema=DATA_QUERY_SCHEMA,
    handler=_handle_data_query,
    check_fn=lambda: True,          # 或检查 duckdb 是否可用
    emoji="DATA",
    max_result_size_chars=200_000,
)
```

**关键**:工具本身是薄的——真正逻辑在 `agent/data_agent/` 服务层。这样 CLI/Web/Gateway 复用同一逻辑。

### 9.3 toolset 定义与"是否默认开"的策略

在 `toolsets.py` 的 `TOOLSETS` 里加:

```python
"data-agent": {
    "tools": [
        "data_source_connect", "data_profile", "data_catalog",
        "data_query", "data_validate", "data_execute_code",
        "data_report", "analysis_audit",
        # 复用通用工具
        "read_file", "write_file", "terminal", "execute_code",
    ],
    "includes": ["file"],
},
```

**开关策略(呼应 `future work.md` 9.2 的未决问题)**——两种,demo 二选一:

- **方案甲(demo 推荐)**:普通 DAClaw 保持通用,**只有进入 data mode 时**才启用 `data-agent` toolset。
  好处:普通用户不被一堆数据工具干扰;演示时"切进 data mode"本身就是一个清晰的动作。
- 方案乙:把 data-agent 工具也塞进 core,DAClaw 默认就是 DataAgent。适合"这台就是数据分析专机"。

demo 用**方案甲**:`DataAgentMode` 激活时 `enabled_toolsets=["data-agent", ...]`,退出即恢复。

### 9.4 全局状态注意(来自 `future work.md` 6.3.2)

`model_tools.py` 有 `_last_resolved_tool_names` 等进程级全局状态,影响普通 agent/子代理/RL。
新工具**只通过 registry/toolset/schema 层接入**,**不在 `handle_function_call` 的分发主流程里塞 data 专属分支**。
(唯一例外是 `execute_code` 已有的 `enabled_tools` 传递,我们复用它,不新增全局态。)

---

## 批次 10:交互界面(CLI + Web,同一后端)

> 原则(呼应架构批次 3.2):CLI 和 Web **都调 `agent/data_agent/` 服务层**,
> 不各写一遍数据逻辑。CLI 命令和 Web API 都是**薄适配**。

### 10.1 CLI:`/data` 系列 slash 命令

在 `daclaw_cli/commands.py` 的 `COMMAND_REGISTRY` 加一个 `CommandDef`(照现有结构):

```python
CommandDef(
    name="data",
    description="DataAgent:数据源、目录、分析、artifact 管理",
    subcommands=["mode", "sources", "connect", "profile", "catalog",
                 "query", "runs", "artifacts", "demo-seed"],
),
```

各子命令行为:

| 命令 | 行为 | 对应服务层 |
|---|---|---|
| `/data mode [on\|off]` | 进/出 DataAgent mode(切 toolset + prompt) | `mode.py` |
| `/data connect <path\|ref>` | 接入数据源 | `sources.py` |
| `/data sources` | 列出已接入的源 | `sources.py` |
| `/data profile <table?>` | 对源/表做 profiling | `workflow.py` |
| `/data catalog [table\|metric]` | 查看/编辑语义目录 | `catalog.py` / `metrics.py` |
| `/data query "<sql>"` | 手动跑一条 SQL(调试用) | `workflow.run_query` |
| `/data runs` | 列出历史 analysis_run | `artifacts.py` |
| `/data artifacts <run_id>` | 展开某次分析的 SQL/代码/结果/图/报告 | `artifacts.py` |
| `/data demo-seed` | **一键生成合成电商数据集**(批次 2.0) | `demo_seed.py` |

**demo 关键**:`/data demo-seed` 让任何人 clone 下来就能复现整场演示。`/data runs` + `/data artifacts` 是"可复现/证据链"的 CLI 出口。

### 10.2 Web:挂在现有 WorkspacePage 上

现状:`web/src/pages/WorkspacePage.tsx`(2534 行)已存在数据工作台;后端有 `/api/workspace/*` 路由
(`/api/workspace/files`、`/preview` 等,已核对 `web_server.py:2728+`)。我们在此之上加**分析相关的呈现**:

新增后端 API(FastAPI,`web_server.py`,复用 `/api/workspace` 前缀或新开 `/api/data`):

```text
GET  /api/data/sources                 已接入数据源
POST /api/data/connect                 接入新源
GET  /api/data/catalog?source=         语义目录(表/关系/指标)
GET  /api/data/runs                    历史分析列表
GET  /api/data/runs/{run_id}           单次分析全链路(plan/queries/results/charts/report/audit)
POST /api/data/query                   跑 SQL(调试/探索)
```

前端在 WorkspacePage 加三个面板(都读上面的 API):

1. **Catalog 面板**:树状展示 源→表→字段(带 semantic_type/grain 标注)+ 指标口径卡片。
2. **Analysis Runs 面板**:历史分析列表 → 点开看**报告 + 每个数字旁的"来源"链接**(点击展开产出它的 SQL + 结果表)。这是"证据链"最直观的可视化。
3. **Validation 徽章**:分析结果上显示 校验 pass/warn/fail 徽章,warn/fail 可点开看 evidence(剧本 C 的"抓谎"在 Web 上一目了然)。

> demo 阶段前端不必大改架构:这三个面板作为 WorkspacePage 内的 tab/section 增量加入即可,
> 复用现有的 API client(`web/src/lib/api.ts`)和 i18n。**不引入新的状态管理**,避免撑爆现有 localStorage 方案(架构批次提到的风险)。

### 10.3 Gateway(加分,剧本 D 用)

不新增 gateway 逻辑——剧本 D 的"定时报告发 Telegram"完全复用现有 `cron/` + `gateway/` +
新增的 `data_report` 产物。gateway 侧只是把 analysis_run 的报告摘要作为消息发出。

### 10.4 三个入口的一致性保证

```text
CLI /data query ─┐
Web /api/data/query ─┼─▶ agent/data_agent/workflow.run_query()  ← 唯一实现
Gateway(经 cron)─┘
```

**同一条 SQL 执行路径、同一套 validators、同一个 artifact 存储**。这既是工程正确性,也是 demo 说服力:
"你在 CLI 看到的数字,和 Web 上、和定时推送里的,是同一次可复现的执行产出的。"

---

> **批次 9~10 完成。** 继续写批次 11~12(路线图、demo 脚本与验收)。

---

## 批次 11:实施路线图 + 风险

> 排序原则:**按"demo 可演示价值 / 实现成本"比值**,先做能最快点亮剧本 A/B/C 的东西。
> 每个里程碑都以"能演示什么"收尾——不追求架构完美,追求**每一步都有可看的东西**。

### 11.1 里程碑(建议 5 步,每步都可独立演示)

**M0 — 地基与数据(0.5~1 天):让 demo 有米下锅**
- [ ] 新建 `agent/data_agent/` 空包骨架(`__init__.py` + 各模块占位)。
- [ ] 写 `demo_seed.py`:固定 seed 生成合成电商数据集,导出 CSV + Parquet + SQLite + DuckDB 各一份。
- [ ] 加 `/data demo-seed` 命令。
- **可演示**:`daclaw data demo-seed` → 生成一套数据 → `ls` 看到文件。虽小,但这是可复现演示的地基。

**M1 — 接入 + 建模(点亮剧本 A)(1~2 天)**
- [ ] `sources.py`:`SqliteAdapter` + `DuckDBAdapter`(+ CSV/Parquet/Excel 经 DuckDB),实现 `list_tables/profile/run_query`。
- [ ] 扩展 `data_profile` 支持 `table` 维度;新增 `data_source_connect` 工具。
- [ ] 扩展 `catalog.py`:主外键推断 + grain/semantic_type 字段。
- [ ] `/data connect`、`/data profile`、`/data catalog` 命令。
- **可演示**:剧本 A 全程——黑盒 sqlite → 自动 profiling + 建目录 → `/data catalog` 看到表和关系,重开仍在。

**M2 — 执行 + Artifact + 生命周期(点亮剧本 B 主干)(2~3 天)**
- [ ] `artifacts.py`:analysis_run 存储(run.json + queries/code/results/charts/report)。
- [ ] `data_query`(ResultTable 信封)+ `data_execute_code` 工具。
- [ ] `workflow.py`:intake→plan→execute→report→persist 编排(先不含硬校验)。
- [ ] `metrics.py` + `data_report`(带证据链约束)。
- [ ] `/data runs`、`/data artifacts` 命令。
- **可演示**:剧本 B——一句话问题 → 自动出报告 → `/data artifacts` 看到每个数字背后的 SQL,重跑一致。

**M3 — 校验 + 抓谎(点亮剧本 C,demo 高潮)(1~2 天)**
- [ ] `validators.py`:6 个确定性校验器(批次 8.1)。
- [ ] `provenance.py`:数字↔artifact 绑定 + `data_report` 拒绝无来源数字。
- [ ] workflow 接入 Validate 阶段(fail 回退重算);升级现有 audit 输入为结构化 artifact。
- [ ] `demo_seed.py` 里**埋雷**(重复行/NULL/时区)。
- **可演示**:剧本 C——埋雷数据上 agent 当场抓出 3 类问题并纠正,`/data validate` 看到红黄灯 + evidence。

**M4 — Web 呈现 + 定时(点亮剧本 D + 可视化)(1~2 天)**
- [ ] `/api/data/*` 后端路由。
- [ ] WorkspacePage 加 Catalog / Runs / Validation 三面板。
- [ ] 剧本 D:把剧本 B 分析封装成 cron 任务 + gateway 推送。
- **可演示**:Web 上点报告数字看来源、看校验徽章;定时任务自动出报告发 Telegram。

> **最小可演示集 = M0+M1+M2+M3**(剧本 A/B/C 全通),约 5~8 天。M4 是加分与"好看"。

### 11.2 依赖与开关

- 新增可选依赖归入 `.[data]` extra:`duckdb`、`pyarrow`、`openpyxl`、(P1)`psycopg`。
  未装时对应 adapter 经 `check_fn` 优雅缺席(照现有 `check_data_requirements` 模式)。
- `.[data]` 并入 `.[all]`;README/文档标注 data mode 需要它。

### 11.3 风险与规避

| 风险 | 影响 | 规避 |
|---|---|---|
| 改动碰到 `run_agent.py` 主循环 | 波及所有入口,回归面大 | **只通过 mode/tool/hook 挂入**;不进 dispatch 主流程加分支(批次 3/9) |
| prompt caching 被 catalog 动态注入破坏 | 变慢变贵 | catalog 走"工具查询",不塞 system prompt(批次 5.5) |
| 大结果撑爆 context | 超限/变慢 | ResultTable 只回样例+摘要,全量落 parquet(批次 7.2) |
| DuckDB/pyarrow 在 Termux/老环境装不上 | 部分平台 data mode 不可用 | 设为可选 extra + 优雅降级;demo 主打 Linux/macOS |
| LLM 生成的 SQL 有害(写操作/删表) | 数据被破坏 | `data_query` 强制只读(拒绝非 SELECT);写操作需显式确认(复用现有 approval) |
| 校验器误报导致 agent 反复重算 | 卡死/超迭代 | validator 分 warn/fail;fail 才回退,且计入 `max_iterations` 预算(现有机制) |
| 文档编码乱码(`future work.md` 9.3 提到) | 观感差 | demo 前统一清一遍输出编码 |
| 子代理 403(本会话已撞到) | 并行/委派失败 | 用修好的 `claude-w --model` 启动;data 分析的子代理显式带模型 |

### 11.4 为将来 RL 铺的路(不在 demo 范围,但零成本预留)

analysis_run 的结构化 artifact(plan/query/result/validator/report)天然是 RL 训练信号。
将来做 DataAgent RL(`future work.md` 第 7 节)时,reward 可拆成
schema/execution/numeric/validation/report 多项——**现在把 artifact 存好,将来直接喂训练**,demo 阶段不必实现。

---

## 批次 12:Demo 脚本与验收清单

> 这一批是"交付物":现场照着念就能演,验收照着勾就能验。

### 12.1 现场演示脚本(约 12~15 分钟)

**开场(1 min)**:一句话定位 + "重点不是它能算,而是它算得**可信、可复现**"。

**Act 0 — 准备(0.5 min)**
```bash
daclaw data demo-seed          # 生成合成电商数据(customers/orders/order_items/products/events)
daclaw --model 'c-o-48[1m]'    # 启动(注意引号)
/data mode on                  # 进入 DataAgent 模式
```

**Act 1 — 接入建模(剧本 A,3 min)**
```
你:这是 ./demo_data/shop.sqlite,帮我搞清楚里面有什么、表怎么关联,建好目录。
```
看点:自动 profiling 每张表 → 推断主外键 → `/data catalog` 展示成果。
**镇场话术**:"注意这个目录是**持久化**的——我关掉重开,它还在,下次分析直接用。"

**Act 2 — 问题到报告(剧本 B,4 min)**
```
你:过去90天,哪些商品品类复购率最高?按月看趋势。
```
看点:agent 读目录 → 定口径 → 跑真实 SQL → 出报告 + 趋势图。
**镇场动作**:`/data artifacts <run_id>` → 点开报告里"34%"这个数 → **展示产出它的那条 SQL 和结果表**。
"这不是模型编的数,是这条 query 跑出来的,我现在重跑给你看——一样。"

**Act 3 — 抓谎(剧本 C,4 min,高潮)**
```
你:算一下总 GMV 和每天的下单量。
```
看点:校验层当场报告——"检测到重复行会让 GMV 虚高 7.8%,已去重";"某月 12% 金额为 NULL,已排除并标注";"时间戳跨时区,已统一 UTC"。
**镇场话术**:"大多数 AI 数据分析会安静地算错。它**抓出了数据里的坑并纠正**,还把局限写进了报告。"

**Act 4 — 沉淀为管道(剧本 D,2 min,收尾)**
```
你:把'每日GMV和新增客户'做成每天9点自动生成,发我Telegram。
```
看点:复用 cron+gateway,展示定时任务已建;(可预录)Telegram 收到报告。
**收尾**:回到 Web WorkspacePage,展示 Catalog / Runs / 校验徽章三面板——"CLI、Web、定时推送,是同一条可复现的执行链。"

### 12.2 验收清单(可勾选,对应批次 1.5 的成功标准)

**功能验收**
- [ ] `data demo-seed` 生成的数据集,4 种格式(CSV/Parquet/SQLite/DuckDB)内容一致。
- [ ] 能接入 ≥4 类源并 profiling(CSV、Excel、Parquet、SQLite/DuckDB)。
- [ ] catalog 能存 表/关系/指标口径,且跨会话留存(重启后 `/data catalog` 仍在)。
- [ ] 剧本 B 问题能自动 plan→execute→report,报告含月度趋势。
- [ ] 报告每个关键数字都有 artifact 引用;`/data artifacts` 能取出对应 SQL。
- [ ] 重跑存下的 SQL,数字与报告一致(可复现)。

**可信度验收(硬指标,确定性)**
- [ ] 埋雷数据集上,`join_grain` / `null` / `time_window` 三个校验器**每次**都报出对应问题(跑 5 次全中)。
- [ ] `data_report` 拒绝无 artifact 来源的数字(构造一个"凭空数字"应被挡)。
- [ ] `data_query` 拒绝非 SELECT(构造一个 `DROP`/`UPDATE` 应被挡)。

**一致性验收**
- [ ] 同一 SQL 在 CLI(`/data query`)、Web(`/api/data/query`)返回一致结果。
- [ ] analysis_run 在 CLI `/data runs` 和 Web Runs 面板都能看到同一条。

**鲁棒性验收**
- [ ] 未安装 `.[data]` extra 时,data 工具优雅缺席,主程序不崩。
- [ ] 空表/坏文件/连不上的源,报清晰错误而非堆栈崩溃。

### 12.3 "如何证明实际可用"的三句话总结(给评审的电梯陈述)

1. **真跑不假装**:每个数字来自真实 SQL/代码执行,artifact 落盘,可一键重跑复现。
2. **会抓错**:确定性校验层在埋雷数据上稳定抓出 join 膨胀、缺失、时区等真实数据陷阱。
3. **能沉淀**:一次分析 = 一个可追溯的 analysis_run;还能一键变成无人值守的定时管道。

---

## 附:待你确认后可能微调的点(汇总)

写作过程中我用了几个"合理默认",都可推翻,列在这里方便你一次性回:

1. **数据领域**:合成**电商**数据集(可换成金融/广告/SaaS/物流)。
2. **必演剧本**:A+B+C 必演、D 加分(可增删)。
3. **卖点**:以剧本 C「抓谎」为高潮(你也可能更想突出数据源广度或可视化)。
4. **受众**:内部技术评审 + 可转客户(影响话术侧重)。
5. **数据源优先级**:DuckDB/Parquet/Excel/SQLite 优先,Postgres 次之,云仓暂缓。
6. **toolset 开关**:方案甲(data mode 才启用 data-agent 工具)。

—— 以上任何一条要改,只改对应批次即可,不影响整体结构。

> **全部 12 批次完成。** 这是一份完整的 demo 策划蓝图,从定位到验收清单闭环。
> 下一步可以:①按你的反馈微调某些批次;②或直接从 **M0(数据种子)** 开始动手实现。

---

## 批次 13:重点体验线 —— 表格 → 美丽可视化 → 对话即时出图 → 一键文档

> 用户追加的核心诉求(原话):"把一个表格放进去 → 非常美丽地可视化 → 我问问题他都能很好回复并直接可视化 → 还能根据表格产生文档。"
>
> 这一批把 demo 的**体验重心**明确压在这条线上。**关键结论:所需的底层能力仓库里几乎都有,不用从零造轮子——重点是把现有的 HTML 报告路径做到"可靠 + 漂亮"。**

### 13.1 已核实的代码事实(决定了实现路径)

| 事实 | 出处(已核对) | 对本需求的意义 |
|---|---|---|
| Web 工作台已用 **`<iframe srcDoc={preview.html} sandbox="allow-same-origin allow-scripts">`** 渲染 HTML | `web/src/pages/WorkspacePage.tsx:741-744` | **这是决定性的**:一个自包含 HTML(内联图表库)能直接在 UI 里渲染、且 **JS 可执行** → 图表能动 |
| 预览类型已含 `table`/`database`/`html`/`text/json`;文件 kind 已含 `report`/`dashboard`/`visual` | 同上,`:443-455`、`:779-782` | 表格预览、报告、仪表盘的**壳子已经在了** |
| 后端 prompt 已指示 agent "**优先生成自包含 HTML 报告**" | `daclaw_cli/web_server.py:2247-2250` | 产品意图已内建,只是缺一条可靠、好看的生成管线 |
| `TablePreview` 组件已存在 | `WorkspacePage.tsx:656` | 上传表格→预览这一步现成 |
| **前端没有图表库**(package.json 无 echarts/recharts/plotly) | `web/package.json` | **不要**装前端图表库;走"agent 生成内联图表的 HTML"路线更省更灵活 |
| **主依赖没有 matplotlib/plotly/python-docx 等** | `pyproject.toml` | 可视化和文档生成的依赖要新增(归入 `.[data]` extra) |
| 有 `data-science`(jupyter-live-kernel)、`diagramming`、`creative` skills | `skills/` | 有现成技能可复用/借鉴 |

**由此定的技术路线**(两个关键取舍):

1. **可视化 = agent 生成"自包含 HTML + 内联图表库",经现有 iframe 渲染。**
   **不**在 React 前端装 echarts/recharts。理由:①iframe 已能跑 JS,零前端改动;②agent 直接产出 HTML 最灵活(任意图表类型、任意布局、可离线);③同一个 HTML 文件既能在 Web 预览、又能存成 artifact、又能当文档导出。**一份产物,三处复用。**

2. **图表库选型:ECharts(内联/CDN)为主。** 理由:免费、好看(默认主题就漂亮)、支持交互(hover/缩放/图例)、单文件内联可离线。备选 Chart.js(更轻)、Plotly(科学感强但体积大)。**默认 ECharts。**

### 13.2 端到端体验流(这是 demo 要演的"爽点线")

```text
① 上传表格           拖一个 CSV/Excel 进 Web 工作台(现有上传 + TablePreview)
      ↓
② 自动理解 + 首屏可视化   agent 自动 profiling → 判断每列语义(时间/类别/度量)
                        → 生成一个"概览仪表盘"HTML(3~4 张最合适的图)→ iframe 渲染
      ↓
③ 对话式追问即时出图    "按月看销售额趋势" / "哪个品类占比最高" / "找出异常值"
                        → agent 跑真实计算(data_query)→ 生成对应图表 HTML → 就地渲染
                        → 每张图旁边可看"产出它的 query"(证据链,承接批次 8)
      ↓
④ 一键生成文档         "把刚才的分析做成一份报告"
                        → 汇总图表 + 结论 + 数据表 → 产出 HTML 报告(可导出 PDF/DOCX)
                        → 存为 analysis_run 的 report artifact(承接批次 7)
```

### 13.3 "美丽"怎么保证(这是最容易翻车的点,要专门治理)

漂亮不能靠碰运气。三条硬措施:

1. **图表模板库(不让模型每次从零写 HTML)**:预置一组经过打磨的 HTML+ECharts 模板
   (折线/柱状/饼图/散点/热力/漏斗/仪表盘布局),放 `agent/data_agent/viz_templates/`。
   agent 只需**选模板 + 填数据 + 微调**,而不是徒手拼 HTML。→ 稳定漂亮、可离线、渲染快。
2. **统一视觉主题**:一套配色/字体/间距 token(承接 DAClaw 已有 `skin_engine.py` 的审美),
   所有图表和报告共用 → 观感一致、专业。
3. **渲染前自检**:生成的 HTML 先做校验(有没有语法错、图表容器有没有绑定数据、数值有没有 artifact 来源),
   不合格就让 agent 修 → 避免"白屏图表"当场翻车。

> demo 卖点话术:"它不是给你一段能跑的代码,是给你一张**能直接放进汇报的图**。"

### 13.4 新增工具(承接批次 9 的注册模式)

| 工具 | 输入 | 输出 | 说明 |
|---|---|---|---|
| `data_visualize` | `{data_ref/sql, chart_type?, title, options?}` | 自包含 HTML(内联 ECharts)+ artifact_id | 核心:把一个结果表变成一张图的 HTML;chart_type 可省(agent/规则自动选) |
| `data_dashboard` | `{panels:[{title, data_ref, chart_type}...], layout?}` | 多图仪表盘 HTML | 首屏概览、多图报告用 |
| `data_document` | `{run_id/sections, format: html\|pdf\|docx}` | 文档文件(报告) | 汇总图表+结论+表格;HTML 为主,PDF/DOCX 可选导出 |

- `data_visualize` / `data_dashboard` 的产物直接喂现有 iframe 预览 → 无需前端改动即可看到。
- 图表用的数据**必须来自 `data_query` 的真实结果**(承接批次 7.2/8.2 的证据链),不许模型编数。

### 13.5 文档生成(第④步)的务实分级

| 层级 | 格式 | 依赖 | demo 优先级 |
|---|---|---|---|
| **L1(必做)** | **自包含 HTML 报告** | 无(浏览器/iframe 直接看) | ⭐ 首选:最快、最漂亮、可交互、可离线、可当网页分享 |
| L2(加分) | **PDF** | `HTML→PDF`(如 playwright/weasyprint,可选 extra) | 客户要"能打印的报告"时 |
| L3(加分) | **DOCX/PPTX** | `python-docx`/`python-pptx`(可选 extra) | 要"能编辑的 Office 文档"时 |

**demo 主打 L1**:HTML 报告本身就非常能打(图表交互 + 排版专业 + 一个文件搞定)。
PDF/DOCX 作为"还能导出成你要的格式"的加分项,依赖装了才启用(优雅降级)。

> 注:代码执行沙箱若预装 matplotlib,也可走"matplotlib 出静态 PNG"的备选路线;
> 但**交互式 HTML 图表(ECharts)在 demo 里明显更亮眼**,故定 HTML 为主、PNG 为备。

### 13.6 与已写批次的衔接(不重复造)

- 数据接入/profiling → **批次 4**(adapters);表格上传走现有 Web 上传 + `TablePreview`。
- 图表数据来源 + 可复现 → **批次 7**(ResultTable + analysis_run);图表存成 artifact。
- "每张图可溯源到 query" → **批次 8**(证据链);这让可视化也"可信"。
- 工具注册 → **批次 9** 的 `registry.register` 模式;新增 3 个工具进 `data-agent` toolset。
- Web 呈现 → **批次 10** 的 WorkspacePage 面板;iframe 预览已具备,增量加"图表/报告"tab。

### 13.7 对路线图的影响(插入批次 11 的里程碑)

这条体验线建议作为 **M2.5**,插在 M2(执行+artifact)之后、M3(校验)之前——
因为"出图"依赖"能跑出真实结果"(M2),但不必等校验(M3)就能演示惊艳效果:

- [ ] **M2.5 — 可视化 + 文档(点亮批次 13 体验线)(2~3 天)**
  - [ ] `viz_templates/`:5~6 个打磨过的 ECharts HTML 模板 + 统一主题。
  - [ ] `data_visualize` / `data_dashboard` 工具 + 渲染前自检。
  - [ ] `data_document`:HTML 报告生成(L1);存为 analysis_run report artifact。
  - [ ] WorkspacePage:上传表格 → 自动概览仪表盘 → 对话追问就地出图。
  - [ ] (加分)PDF/DOCX 导出,依赖可选 extra。
  - **可演示**:拖一个 Excel 进去 → 秒出漂亮概览仪表盘 → "按月看趋势"即时出交互折线图 → "生成报告"得到一份图文并茂的 HTML(可导 PDF)。

### 13.8 这条线的验收(补充批次 12)

- [ ] 拖入一个 CSV/Excel,能在 ≤1 个来回内看到**自动生成的概览可视化**(不用用户指定图表类型)。
- [ ] 至少 5 种图型可用(折线/柱状/饼/散点/热力),且**自动选型合理**(时间→折线、占比→饼/柱等)。
- [ ] 对话追问"按 X 看 Y"能**就地出对应图**,图上数字可溯源到 query。
- [ ] "生成报告"产出一份自包含 HTML(图表 + 结论 + 数据表),在 Web 内直接渲染。
- [ ] 报告能导出为 PDF(装了可选 extra 时);未装时给清晰提示而非报错。
- [ ] 同一份报告 HTML 拿到浏览器单独打开也正常(自包含、可离线、可分享)。

### 13.9 需要你拍板的几点

1. **图表库**:默认 **ECharts**(免费/好看/可交互/可内联)。你有偏好吗?(Chart.js 更轻 / Plotly 科学感强 / 还是无所谓)
2. **文档格式优先级**:默认 **HTML 报告为主**,PDF/DOCX 加分。你更需要哪种?
   - 如果 demo 受众要"能打印/能塞进邮件附件" → 我把 **PDF** 提到必做。
   - 如果要"业务同事能改" → 提 **DOCX**。
3. **"美丽"的风格基调**:默认走 DAClaw 现有 skin 的审美(简洁专业)。你有品牌色/参考样式吗?有的话给我,模板直接对齐。
4. **首屏自动可视化的激进程度**:上传表格后是**自动直接出一屏仪表盘**(激进、惊艳,但可能不完全命中用户想看的),还是**先给一句"我看到这些列,建议看这几个视角",用户点了再出图**(稳妥)?我倾向**自动出概览 + 顶部给"换个视角"入口**,兼顾惊艳与可控。

---

> **批次 13 完成。** 至此:12 批主体蓝图 + 1 批(你追加的)可视化/文档体验线,已构成完整策划。
> 下一步建议:确认 13.9 的四个选择,然后从 **M0 → M1 → M2 → M2.5** 推进——M2.5 就是你最想要的那条"上传即美图、对话即出图、一键成文档"的线。

---

## 实施状态(2026-07-06 已落地 M2.5)

> 用户拍板"立刻实现,默认 API key 用仓库默认"。以下已**写好并逐环节验证通过**(在 conda `agent` 环境 Python 3.13,duckdb 1.3.2)。

### 已交付的代码

| 模块 | 文件 | 说明 |
|---|---|---|
| 统一执行层 | `agent/data_agent/engine.py` | DuckDB 后端;CSV/TSV/Parquet/Excel/SQLite/DuckDB 全部离线可读、统一 `run_query`→`ResultTable`;只读校验;`profile_table` 带列语义 |
| 结果信封 + 可复现 | `agent/data_agent/artifacts.py` | `ResultTable`(样本进上下文、全量落盘)+ `AnalysisRun`/`ArtifactStore`(query/chart/report 落 `$DACLAW_HOME/analysis_runs/`) |
| 可视化 | `agent/data_agent/viz.py` + `theme.py` + `assets/echarts.min.js` | 自动选图型(时序→折线、占比→饼、排名→柱等);自包含内联 ECharts HTML;统一主题;`self_check_html` 防白屏 |
| 首屏仪表盘 | `agent/data_agent/overview.py` | 从表结构确定性生成 KPI + 趋势 + 分类图(无需 LLM),数字全来自真实 query |
| 文档 | `agent/data_agent/document.py` | 叙述+图表+表格+局限 → 自包含 HTML 报告;PDF(playwright/系统 Chrome/weasyprint)、DOCX(python-docx)优雅降级 |
| 种子数据 | `agent/data_agent/seed.py` | 固定 seed 电商数据集(5 表);CSV/SQLite/Parquet/Excel 四格式;内置可被校验抓到的埋雷 |
| 运行上下文 | `agent/data_agent/run_context.py` | 进程级"当前分析 run",让一次分析内的工具共享 artifact |
| 工具封装 | `tools/data_agent_tools.py` | `data_connect`/`data_query`/`data_visualize`/`data_dashboard`/`data_document`,薄封装 |

### 已接线

- `toolsets.py`:新增 `data-agent` toolset;5 个新工具同时进 `_DACLAW_CORE_TOOLS`(demo 默认可用)。
- `daclaw_cli/commands.py` + `cli.py`:新增 `/data [demo-seed|runs|artifacts|sources]`。
- `daclaw_cli/web_server.py`:升级 web 数据分析 prompt,优先引导用新的 DataAgent 工具(生成的 HTML 自动落 workspace,现有 iframe 预览直接可见)。
- `pyproject.toml`:新增 `[data]` extra(duckdb/pandas/openpyxl/pyarrow);修正 `packages.find` 让 `agent.data_agent` 子包被打包;`assets/*.js` 纳入 package-data;`all` 含 `[data]`。

### 已验证(端到端)

1. 上传多 sheet Excel → `data_connect` 出 5 张表 ✅
2. `data_dashboard(table=...)` 秒出 3 图概览仪表盘,`render_check` 通过 ✅
3. 对话式 `data_visualize(sql=...)` 出交互折线图,图下附产出它的 SQL(证据链)✅
4. `data_document(...)` 出 5 段图文报告 + PDF(系统 Chrome 导出成功)✅
5. `analysis_run` 记录 4 queries / 2 charts / 1 report,可复现 ✅
6. 用真实 Chrome headless 截图肉眼确认:配色统一、图表专业、非白屏 ✅
7. 现有测试回归:`test_toolsets`/`test_project_metadata`/`test_packaging_metadata` 全绿(27 passed);model_tools 的 3 个失败经 stash 对照证明为**环境缺 firecrawl 的既有问题**,与本次改动无关 ✅

### 关于 13.9 的四个选择——已按默认落地(可随时改)

1. 图表库:**ECharts**(已内联,离线可用)。
2. 文档格式:**HTML 为主**,PDF/DOCX 作为可选导出(本机 PDF 已通)。
3. 视觉基调:teal 主色对齐 README 品牌徽章 + 统一 `theme.py`。
4. 首屏激进度:**自动出概览仪表盘**(`data_dashboard(table=...)` 一步到位)。

> 若要改动其中任意一条,只需告诉我——它们都集中在 `theme.py` / `viz.py` / `document.py`,改动面很小。







