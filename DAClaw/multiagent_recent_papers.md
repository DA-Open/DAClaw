# Multi-Agent 论文调研

## 1. 调研目的

目前 DAClaw 已经有一定的 multi-agent 能力，但它更像是“工具级 delegation”，不是完整的 multi-agent 架构：

有一个主agent进行在普通 tool-calling loop 中决定是否调用 delegate_task，然后这个delegate_task 根据任务创建一个或多个子 AIAgent，然后子 AIAgent 独立完成任务。主agent只拿到子任务 summary 和少量 tool trace，再继续自己的 loop。

- `delegate_task`：父 agent 可以把子任务交给子 agent，子 agent 独立跑一轮 `AIAgent.run_conversation()` 后把总结返回给父 agent。
- `mixture_of_agents`：多个模型/agent 并行回答，再由聚合器综合。
- `data_profile`、`data_catalog`、`analysis_audit`：已经有 DataAgent 方向的工具，但还没有形成“数据理解 agent、分析 agent、验证 agent、报告 agent”的稳定角色层。

要从data framework的角度去考虑，而不是工程方面的角度。
目前在做的 muti-agent 在做什么内容？有哪些可以值得借鉴的地方？有哪些模块可以额外进行添加？初步考虑的角度如下所示：

1. 首先任务拆分（是否需要调用子 agent，什么样的任务需要调用子 agent，什么样的任务不需要调用子 agent？）
2. 子 agent 的角色如何定义（是按照分析步骤还是什么别的方案去进行划分？）
3. 子 agent 之间的配合是怎么样子的？
4. 最后返回到主agent的时候的答案应该怎么进行组织？

## 2. 智能体分配

### 2.1 Magentic-One: A Generalist Multi-Agent System for Solving Complex Tasks

- 链接：https://arxiv.org/abs/2411.04468
- 类型：通用 multi-agent 编排系统，采用主控+分工的架构设计

核心思想：

Magentic-One 使用一个 Orchestrator 作为总控 agent，负责任务规划、进度追踪、错误恢复和重新规划。其他 agent 是专门角色，例如浏览器 agent、文件 agent、代码执行 agent 等。它强调的是“主控编排 + 专门执行”，而不是所有 agent 平等讨论，**模块化的思想**。

可以借鉴的点：

- 对于目前的项目来说已经有一个主 `AIAgent`，也已经有 `delegate_task`。
- 可以把现有 delegation 从“模型自己想起来才调用”升级为“特定任务触发特定角色”。
- DataAgent 场景下可以设计：
  - `DataProfilerAgent`：理解数据结构、缺失值、字段类型。
  - `AnalysisPlannerAgent`：拆分析问题、设计统计或 SQL 步骤。
  - `CodeExecutorAgent`：执行 Python/SQL。
  - `VerifierAgent`：检查结果是否可复现、是否过度推断。
  - `ReportAgent`：生成最终报告。

思路：

- Orchestrator 明确维护计划和状态。
- 专门 agent 可插拔。
- 不要求改底层模型调用方式，适合渐进式改造。


### 2.2 Talk Structurally, Act Hierarchically: A Collaborative Framework for LLM Multi-Agent Systems

- 链接：https://arxiv.org/abs/2502.11098
- 类型：结构化通信 + 层级式修正

核心思想：

TalkHier 关注 multi-agent 协作中的通信质量。

从两个角度去进行的思考：1、它认为很多 multi-agent 系统效果不稳定，是因为 agent 之间只是自由文本对话，缺少结构化的信息交换协议。2、评估器要么顺序执行（后一个被前一个影响），要么能力同质化（无法平衡不同意见），导致输出存在错误、幻觉和偏见

因此它引入结构化通信（背景+格式+消息）和层级式 refinement，让 agent 的意见、证据、错误和修正路径更清晰。

思路：

- 或许子 agent 返回给父 agent 的内容不应该只是自然语言总结，可以改成结构化 JSON。

改进方向：

**固定结构化输出、各自为战**

- 扩展 `delegate_task` 的返回 schema。
- 对 `analysis_audit` 增加结构化输入，让审计工具检查 claim-evidence 是否匹配。
- 在 data analyst personality 下要求所有分析型子任务输出固定结构。

### 2.3 MasRouter: Learning to Route LLMs for Multi-Agent Systems

- 链接：https://arxiv.org/abs/2502.11133
- 类型：multi-agent 路由、成本控制、角色选择

核心思想：

MasRouter 讨论的问题是：multi-agent 系统不应该每次都固定拉满 agent，而应该判断当前任务是否需要协作、需要哪些角色、每个角色使用什么模型。它把 collaboration mode、role allocation 和 model routing 放进统一路由框架。

在决定用什么协作方式和角色的时候，使用的不是LLM进行决策，而是采用一个轻量级的神经网络进行决策


思路：

这里在分配角色和决定架构的时候可以通过网络自己来判断采用什么架构，并不是所有的问题都是统一的类似的架构，但是架构是人为定义的，也可能会存在一些缺陷问题。

## 3. 聚合、辩论与投票

### 3.1 Mixture-of-Agents Enhances Large Language Model Capabilities

- 时间：2024-06
- 链接：https://arxiv.org/abs/2406.04692
- 类型：多模型并行生成 + 分层聚合

核心思想：

MoA 使用多层 agent。每一层有多个 LLM agent 并行生成答案，下一层 agent 会读取上一层所有输出，再生成更强的结果。它更像“集体生成 + 聚合”，不是任务分工型 multi-agent。


采用并行架构，在协作上感觉没有很强的能力，每一个agent都是不可控的，这样子链条传递下去一个是存在在聚合的时候的fake news的信息成本过大，另一个是每一层的agent之间并没有协作，应该无法解决较为困难的问题，都是简单的问题。

思路：
- MoA 可以用在“解释和报告写作”阶段。
- 不用 MoA 直接替代数据计算。
- 数据计算应该由工具或代码执行完成，MoA 只负责观点综合、风险提示和表达润色。

### 3.2 On scalable oversight with weak LLMs judging strong LLMs

- 时间：2024-07
- 链接：https://arxiv.org/abs/2407.04622
- 类型：debate、consultancy、judge 评估协议

核心思想：

这篇研究让模型扮演辩论者、顾问和裁判，用来分析不同监督协议的效果。它不是传统应用型 multi-agent 框架，但对“让一个 agent 检查另一个 agent 的结论”很有参考价值。

两个agent进行辩论能够让judger有更好的判断能力。

思路：

在整体链路上没啥作用感觉，但是在最后的结论的检查的时候可以加入一个反对的agent来判断生成的结论到底对不对，是一个可行的思路。

- 对数据分析结论，可以引入一个“反方 verifier”：
  - 主分析 agent 给出结论。
  - verifier 专门找漏洞、反例、统计误用、数据泄漏。
  - judge 或主 agent 再整合。
- 主要是判断当前的信息的数据结论是不是正确的

### 3.3 MALLM: Multi-Agent Large Language Models Framework

- 时间：2025-09
- 链接：https://arxiv.org/abs/2509.11656
- 类型：multi-agent debate 框架与配置化实验

核心思想：

MALLM 提供一个面向研究的 multi-agent debate 框架，可以配置 persona、response generator、discussion paradigm 和 decision protocol，例如 voting、consensus 等。

识别出 MAD 研究中三个关键但常被纠缠在一起的组件（Agent，讨论范式和决策的协议），构建了一个能独立操控和评估每个组件的开源框架，从而让研究者能够系统性地理解各组件及其交互对多智能体辩论效果的影响。

思路：
个人感觉作用并不是很大，对于目前的项目的影响不是很强


## 4. 评估与可观测性

### 4.1 Agent-as-a-Judge: Evaluate Agents with Agents

- 时间：2024-10
- 链接：https://arxiv.org/abs/2410.10934
- 类型：agent 过程评估、自动化 judge

核心思想：

传统 LLM-as-a-Judge 往往只看最终答案，而 agent 系统真正重要的是中间过程：有没有正确调用工具、有没有遗漏需求、有没有错误恢复、有没有满足层级化需求。Agent-as-a-Judge 用 agent 来评估 agent，可以提供更细粒度的过程反馈。

主要是更新了一个新的agent的评估的框架，不再是简单的使用llm-as-judge。和人类基线的评估效果接近一致。

思路：

目前审计的方向可以继续升级和改进，也就是检查它在审计数据的时候对于工具的调用过程，检查数据来源和分析是否可靠。

- 当前 `analysis_audit` 已经有最终报告审计的方向。
- 可以继续升级为：
  - 检查工具调用过程。
  - 检查数据来源是否覆盖。
  - 检查每个用户需求是否被满足。
  - 检查是否有 unsupported claim。
  - 检查是否存在“看起来像分析但没有真实计算”的回答。

- 数据分析 agent 最大的问题不是不会写报告，而是可能：
  - 没有真的读取数据。
  - 把样本结论说成总体规律。
  - 忽略缺失值和异常值。
  - 混淆相关和因果。
  - 没有记录分析步骤。

因此，DAClaw 的 multi-agent 改进不只要“更多 agent”，更要有 judge/audit agent。

价值较高。

### 4.2 MultiAgentBench: Evaluating the Collaboration and Competition of LLM agents

- 时间：2025-03
- 链接：https://arxiv.org/abs/2503.01935
- 类型：multi-agent benchmark、协作/竞争评估

核心思想：

MultiAgentBench 不只看任务是否完成，还评估 collaboration 和 competition 的质量。它比较了 star、chain、tree、graph 等不同协作拓扑，以及 group discussion、cognitive planning 等策略。

思路：

对于这些bench来说的话，评估的是内部的协作的质量，不仅仅是最终的结果，所以要是适配这类bench的话，还要记录中间是否调用了工具等。

## 5. DataAgent 与数据分析方向

### 5.1 DAgent: A Relational Database-Driven Data Analysis Report Generation Agent

- 时间：2025-03
- 链接：https://arxiv.org/abs/2503.13269
- 类型：关系数据库驱动的数据分析报告 agent

核心思想：

DAgent 面向关系数据库分析报告生成。它把自然语言分析问题拆成多个相对独立的子查询，从数据库中检索关键信息，再综合成完整、正确、简洁的分析报告。它也提出了数据集和评估指标。

架构上采取三段：Planning -> Tools -> Memory,首先是规划阶段，agent会把用户的分析问题拆成几个子问题；之后调用Tools进行查找并且声称报告，之后通过memory来进行持久化

思路：

可以参考问题分解的思路，把复杂的任务进行分解。

### 5.2 Data Agent: A Holistic Architecture for Orchestrating Data+AI Ecosystems

- 时间：2025-07
- 链接：https://arxiv.org/abs/2507.01599
- 类型：DataAgent 架构综述/概念框架

核心思想：

这篇从整体架构角度讨论 Data Agent：如何理解数据、查询、环境和工具，如何编排 pipeline/workflow，如何执行、优化和反思。它不是只讲一个单点工具，而是把 Data+AI 生态中的 orchestration 当成核心问题。

选择 agent、子任务划分、DAG架构、动态修正pipeline，有些架构上的改变

思想：

这样就感觉很像是从工程角度进行改进当前的agent，构成一个新的workflow，如果是从这个角度去改进的话可以参考文中的架构构成。

- 很适合用来定义 DAClaw 的 DataAgent 长期目标。
- DAClaw 不应该只是在 prompt 里说“你是 data analyst”，而应该让 data analyst 模式拥有：
  - 数据目录。
  - 数据 profiling。
  - schema/metadata 记忆。
  - 分析计划。
  - 工具执行。
  - 结果审计。
  - 报告产物管理。

### 5.3 Towards an AI co-scientist

- 时间：2025-02
- 链接：https://arxiv.org/abs/2502.18864
- 类型：科学发现 multi-agent 系统

核心思想：

AI co-scientist 使用 generate、debate、evolve 的方式进行假设生成。它通过异步任务执行、锦标赛式演化和多 agent 讨论来提高假设质量。

IDEA的生成过程，在探索性问题上的muti-agent的探索逻辑，可以作为一个架构来参考，比如在做这类的数据探索类问题的时候，采取这种多个角色和验证的方式来进行分析.

思想：

- 适合借鉴到“探索式数据分析”而不是普通问答。
- 当用户不是问一个确定答案，而是问“数据里有什么值得关注的现象”时，可以采用：
  - 多个 hypothesis agent 生成不同假设。
  - critique agent 批评假设。
  - executor agent 验证假设。
  - reporter agent 汇总最可靠发现。

需要注意：

- 这类模式成本较高。
- 更适合复杂分析任务或研究型任务，不适合每次普通数据查询都启用。

## 6. Summary

DAClaw 当前已经有 multi-agent 的入口，但还没有形成稳定的 multi-agent 架构。最值得改进的不是把 agent 数量堆上去，而是把 data analyst 模式改造成“可路由、可分工、可审计、可追溯”的 DataAgent 团队：

```text
User Task
  -> Router
  -> Orchestrator
  -> Data Profiler / Planner / Executor / Verifier / Reporter
  -> Evidence Store
  -> Analysis Audit
  -> Final Answer
```

改进：

1、muti-agent的组织形式，采用结构化交流和结构内容

2、引入judge agent来检查分析的过程和结论的正确性

## 7. My Idea

从刷benchmark的角度来说的话，目前的结构在调用的循环是主体agent去决定是不是要调用tools，然后临时创建一个或者多个隔离的子 agent，然后子agent独立完成任务之后返回summary，父agent继续生成答案。

```text
主 AIAgent 在普通 ReAct 循环里，
如果模型选择调用 delegate_task，
就临时创建一个或多个隔离子 agent；
子 agent 独立完成任务并返回 summary；
父 agent 再继续生成答案。
```

我的想法是现在的这样的调用逻辑确实是很灵活的能够调用不同的agent进行调用，但是互相之间的通信和角色固定等方面都没有做到，参考上面的论文中的思想的话，要是从workflow的角度去更改的话是不是一个可行的方法去刷benchmark？因为要组织起来mulitagent，对于这种结构化的数据分析是不是要固定一些角色更加方便审查等，要不然也刷不了benchmark。预想的结构如下所示：

```text
                +--------------------+
                | Benchmark/User Task |
                +---------+----------+
                          |
                          v
                +--------------------+
                | Data Orchestrator  |
                +---------+----------+
                          |
                          v
                +--------------------+
                | DataContext Build  |
                +---------+----------+
                          |
                          v
                +--------------------+
                | PlannerAgent       |
                +---------+----------+
                          |
                          v
                +--------------------+
                | Execution DAG      |
                +---------+----------+
                          |
          +---------------+----------------+
          |               |                |
          v               v                v
   Profile Step      Query Step       Modeling Step
          |               |                |
          +---------------+----------------+
                          |
                          v
                +--------------------+
                | EvidenceStore      |
                +---------+----------+
                          |
                          v
                +--------------------+
                | VerifierAgent      |
                +----+----------+----+
                     |          |
                  pass        fail
                     |          |
                     v          |
          +------------------+  |
          | AnswerFormatter  |  |
          +--------+---------+  |
                   |            |
                   v            |
          +------------------+  |
          | Final Answer     |  |
          +------------------+  |
                                |
                                v
                         Replan/Re-execute

```