# 最近半年 Multi-Agent 与 Claw 系论文整理

> 时间窗口：2025-10-23 至 2026-04-23。  
> 关注目标：不是设计 benchmark，也不是立刻适配 benchmark，而是从近期 multi-agent 与 Claw 系评测论文中观察大家正在改进什么能力，反推 DAClaw 作为 general agent framework 可以加入哪些“现象级”增强。

## 总体趋势

最近半年相关论文的方向已经不再是简单地“多开几个 agent 投票”。更明显的趋势是：

1. **从投票转向过程验证**：不只看最终答案，而是检查每一步推理、工具调用和环境状态。
2. **从固定角色转向动态上下文**：multi-agent 的关键不只是角色分工，而是谁在什么时候看到什么上下文。
3. **从失败归因转向可验证干预**：失败后不是让模型猜哪里错，而是修改某个消息、计划或动作后重放验证。
4. **从静态评测转向真实环境轨迹**：Claw 系 benchmark 更关注真实网页、办公服务、写操作、安全性和轨迹审计。
5. **从工具列表转向工具语义**：工具越多，越需要 agent 理解工具之间的协作关系，而不是只靠检索工具名。
6. **从普通回答转向 evidence-grounded output**：深度研究、数据分析、网页任务都要求回答或动作能追溯到证据。

对 DAClaw 来说，比较自然的改进方向不是重写架构，而是在 Hermes-like general agent 上加入：

```text
Context Structuring
+ Evidence / State Tracking
+ Verification / Repair
+ Skill or Workflow Distillation
+ Safer Write-Action Guard
```

## 第一类：Benchmark 相关论文

调研当前 agent benchmark **如何设置任务、如何制造困难、如何判断 agent 是否真的完成了任务**。对 DAClaw 来说，它们的价值是帮助我们反推 general agent framework 应该补哪些通用能力。

### 1. ClawBench: Can AI Agents Complete Everyday Online Tasks?

- 时间：2026-04-09
- 链接：https://arxiv.org/abs/2604.08523
- 类型：Claw 系真实网页任务 benchmark

Benchmark 怎么设置：

- 任务不是放在静态 sandbox，而是放在真实生产网站上。
- 共 153 个 everyday online tasks，覆盖 144 个 live platforms 和 15 个类别。
- 任务包括购物、预约、提交申请、填写表单、处理在线服务等。
- agent 需要真的浏览网页、读取页面、填写字段、跨页面导航。
- 为避免真实副作用，benchmark 只拦截最终提交请求，前面的流程尽量保持真实。
- 评测会记录 session replay、screenshots、HTTP traffic、agent reasoning traces、browser actions 等行为数据。

它刻意设置的困难问题：

- **真实网页动态性**：cookie banner、弹窗、登录墙、异步加载、页面跳转、表单校验都会出现。
- **文档到网页表单映射**：任务经常要求从用户提供的文档里抽取信息，再填入网页字段。
- **多步 workflow**：不是点一个按钮，而是搜索、选择、填写、确认、跳转、再填写。
- **write-heavy 操作**：需要大量准确填写字段，任何一个字段错都可能导致任务失败。
- **真实副作用风险**：购物、预约、申请等任务如果真的提交会有现实后果，所以需要 interception。
- **跨平台泛化**：144 个网站意味着 agent 不能只记住某个固定 UI。

典型任务形式：

```text
给 agent 一份用户简历或个人信息文档，
要求它去某个真实招聘网站提交申请：
  1. 打开目标网站
  2. 找到对应职位
  3. 从文档中抽取姓名、经历、教育背景、联系方式
  4. 填写多页申请表
  5. 上传必要文件
  6. 到最终提交前被 benchmark 拦截
```

最后结论：

当前 agent 离可靠完成日常在线任务还有明显距离。论文报告 Claude Sonnet 4.6 只能完成 33.3% 的任务，说明真实网页环境中的动态性、多步流程、文档到表单映射和写操作安全仍是巨大挑战。

思考：

其实可以发现对于这类测试的benchmark，其实也是包含了很多对于数据的分析和搜集的内容的，例如从文档中需要抽取出来姓名和经历等内容，我认为其实也是属于DA的领域范畴的。

文中没有明显指出当前agent的能力低下的原因是什么，在评测的时候采用的评测机制是评估和人类的trace是否是一致的。

- 这类的 Claw 类任务本质上要求 agent 把非结构化信息变成结构化状态，在传递信息的是否也可以采用结构化的信息传递。

### 2. Claw-Eval: Toward Trustworthy Evaluation of Autonomous Agents

- 时间：2026-04-07
- 链接：https://arxiv.org/abs/2604.06132
- 类型：可信 autonomous agent 评测

Benchmark 怎么设置：

- 共 300 个 human-verified tasks，覆盖 9 个类别。
- 分成三大组：
  - General service orchestration：服务编排、数据检索、跨系统操作。
  - Multimodal perception and generation：视频、图片、文档、网页/SVG/视频生成。
  - Multi-turn professional dialogue：STEM、法律、教育、商业等专业多轮对话。
- 每个任务都有三阶段生命周期：
  - Setup：注入文件、数据、mock services，但不注入评分脚本。
  - Execution：agent 在隔离环境里调用工具完成任务。
  - Judge：任务结束后才注入评分器和参考答案。
- 每个 action 通过三类证据记录：
  - execution traces
  - service-side audit logs
  - environment snapshots
- 用 2,159 个 fine-grained rubric items 做轨迹感知评分。

它刻意设置的困难问题：

- **轨迹不可伪造**：agent 不能只说“我完成了”，评分器会看工具日志和最终环境状态。
- **安全约束嵌入正常任务**：不是单独做安全题，而是在正常工作流中设置禁止动作。
- **鲁棒性错误注入**：mock service 会模拟 API timeout、malformed response、rate limit 等错误。
- **跨模态任务**：同一 benchmark 同时包含文本、文件、网页、图片、视频、多轮对话。
- **多次运行稳定性**：用 Pass@k 和 Pass^k 区分“偶然成功”和“稳定成功”。

典型任务形式：

```text
给 agent 一个客户服务任务：
  1. 从 CRM mock service 检索客户记录
  2. 根据政策文档判断能否回复/赔付/升级
  3. 撰写合规回复
  4. 通过正确渠道发送或保存
  5. 过程中不能泄露凭证，不能给未授权对象发邮件
  6. 如果某个 API 临时失败，agent 需要换策略或重试恢复
```

评分不会只看最后有没有一封邮件，而会看：查了哪个客户、调用了哪个 API、是否触碰禁区、最终服务状态是否正确。

最后结论：

只看最终答案的评测是不可靠的。论文发现 trajectory-opaque evaluation 会漏掉 44% 的安全违规和 13% 的鲁棒性失败。受控错误注入主要降低一致性而不是最高能力，Pass^3 最多下降 24%，说明稳定性比单次成功更重要。

思路：

安全、鲁棒性、过程评测、多模态。这个 bench 的特点。整体上分数还是挺高的，sonnet 4.6可以达到70多分。

鲁棒性、过程透明，这两点提取的关键词。


### 3. ClawsBench: Evaluating Capability and Safety of LLM Productivity Agents in Simulated Workspaces

- 时间：2026-04-06，2026-04-08 修订
- 链接：https://arxiv.org/abs/2604.05172
- 类型：办公生产力 agent benchmark，与 OpenClaw/Claw 生态直接相关

Benchmark 怎么设置：

- 用高保真 mock workspace 替代真实办公服务。
- 包含 5 个 mock services：
  - Gmail
  - Slack
  - Google Calendar
  - Google Docs
  - Google Drive
- 每个服务都有 REST API、SQLite 后端、真实风格 seed data、edge cases 和 safety traps。
- 共 44 个 structured tasks，其中包括单服务任务、跨服务任务、安全关键任务。
- 任务在 Docker 里执行，支持 deterministic snapshot/restore。
- 论文还显式控制 agent scaffold：
  - domain skills 开/关
  - meta prompt 开/关
- 因此它不仅评估 model，也评估 skill scaffold 对能力和安全的影响。

它刻意设置的困难问题：

- **跨服务状态追踪**：例如从 Gmail 找信息，再写到 Calendar 或 Slack。
- **实体对齐**：人名、邮箱、频道、文档、日程对象容易混淆。
- **写操作安全**：发邮件、改文档、分享文件、创建日历都可能造成风险。
- **权限与隐私陷阱**：Drive 分享、内部财务信息、合同文档等不能随便转发。
- **prompt injection**：邮件、文档评论、CC 字段里可能藏有恶意指令。
- **过度安全与过度执行并存**：agent 可能该做的不做，也可能不该改的合同偷偷改了。
- **multi-service 更危险**：项目页报告跨服务任务比单服务更难，而且 unsafe action 更多。

典型任务形式：

```text
用户要求：
“根据最近邮件里的会议安排，帮我在日历里创建事件，
并在 Slack 频道通知相关同事。不要邀请外部人员。”

agent 需要：
  1. 在 Gmail 中找到相关邮件
  2. 抽取时间、地点、参会者、主题
  3. 判断哪些人是内部成员
  4. 在 Calendar 创建正确事件
  5. 在 Slack 正确频道发送通知
  6. 避免把外部邮箱加入日程或泄露敏感信息
```

这类任务难在：同一个信息要跨 Gmail、Calendar、Slack 三个服务流转，并且每一步都是有副作用的写操作。

最后结论：

即使有 full scaffolding，agents 的 task success rate 只有 39%-64%，unsafe action rate 仍有 7%-33%。在 OpenClaw 上，top five models 的成功率只差约 10 个百分点，但 unsafe action rate 从 7% 到 23% 不等，说明能力和安全不是同一个维度。论文还总结了 8 类反复出现的不安全行为，包括 multi-step sandbox escalation 和 silent contract modification。

对 DAClaw 的启发：

多服务进行交流，安全性这个问题在上面的两个bench中都有所涉及，是否是一个值得关注的点？
但是依旧没有指出到底是哪里出现了问题，依旧是总体上的结论，从结果进行出发的，不清楚是从那个地方开始出错的，是数据获取上最初就出错了还是后面的过程中出现了问题。

IDEA：可以加上verifier和guard去专门针对安全性进行操作？结合之前的数据的分析的话。

- 对办公/日常任务，multi-agent 不一定要多人执行，而可以是：
  - main actor 执行动作
  - verifier 检查收件人、时间、文档对象、权限
  - guard 在写操作前阻断风险动作

## 第二类：Multi-Agent 相关论文

这一类论文重点是最近 multi-agent 方法本身在怎么改进。它们更适合帮助我们思考 DAClaw 是否应该在现有 `delegate_task` 之外加入新的 scaffold，例如上下文对齐、过程验证、失败干预、workflow distillation 等。

### 1. DoVer: Intervention-Driven Auto Debugging for LLM Multi-Agent Systems

- 时间：2025-12-07，2026-01-31 修订
- 链接：https://arxiv.org/abs/2512.06749
- 类型：multi-agent 自动调试与失败修复

主要内容：

DoVer 研究 LLM multi-agent systems 为什么难以调试。作者指出，长轨迹、多分支交互导致失败原因很难定位；当前常见做法是让 LLM 看日志做 failure localization，但这种 log-only debugging 缺少验证，而且把错误归因到单个 agent / 单一步骤往往并不成立。

做了什么：

- 提出 intervention-driven debugging framework。
- 先生成 failure hypothesis，再通过 targeted intervention 主动验证。
- intervention 可以是：
  - 修改消息
  - 修改计划
  - 修改某个 agent 的输出
  - 重放后续执行
- 不把 attribution accuracy 作为唯一目标，而是看 failure 是否被修复或里程碑是否有进展。
- 在 Magentic-One 的 GAIA / AssistantBench 派生数据上评估。
- 在 AG2 + GSMPlus 上验证跨框架泛化。

最后结论：

DoVer 能把 18%-28% 的失败任务翻转为成功，带来最高 16% 的 milestone progress，并能验证或反驳 30%-60% 的失败假设。在 AG2/GSMPlus 上恢复 49% 的失败 trial。结论是：intervention 比纯日志归因更适合提升 agentic system 的可靠性。

对 DAClaw 的启发：

- DAClaw 可以把失败修复做成 framework 能力，而不是“再让模型试一次”。
- 需要支持 checkpoint、trajectory segment、局部重放和 targeted repair。
- 对 `delegate_task` 的改进可以不是增加更多子 agent，而是给子 agent 输出和工具轨迹加可干预结构。
- 可以设计 `RepairAgent`：读取失败轨迹，提出局部改动，然后由主 agent 验证。

### 2. Context Learning for Multi-Agent Discussion

- 时间：2026-02-02
- 链接：https://arxiv.org/abs/2602.02350
- OpenReview：https://openreview.net/forum?id=EUu8TILWpR
- 类型：multi-agent discussion 的动态上下文学习

主要内容：

这篇论文关注 Multi-Agent Discussion 中的 discussion inconsistency。作者发现多个 LLM agent 讨论时，经常因为各自上下文不一致，无法形成连贯解法，或者过早收敛到错误多数意见。

做了什么：

- 提出 M2CL，即 multi-LLM context learning。
- 为每个 agent 学习一个 context generator。
- context generator 会在每一轮讨论中动态生成 context instruction。
- 通过自动信息组织与 refinement 控制：
  - context coherence
  - output discrepancy
  - 避免 majority noise
- 在 academic reasoning、embodied tasks、mobile control 上评估。

最后结论：

M2CL 比现有方法提升 20%-50%，同时具有较好的迁移性和计算效率。结论是：multi-agent 的关键不只是角色设置，而是动态上下文对齐。

对 DAClaw 的启发：

- 当前 `delegate_task(goal, context)` 太薄，真正应该传入 structured context pack。
- 对 DAClaw 更适合的改进不是“多 agent 自由讨论”，而是：

```text
task goal
+ task state
+ evidence summary
+ tool history
+ known constraints
+ failed attempts
+ expected output schema
```

- 也就是说，DAClaw 的 subagent 强化点在 context engineering，而不是 agent 数量。

### 3. DynaDebate: Breaking Homogeneity in Multi-Agent Debate with Dynamic Path Generation

- 时间：2026-01-09
- 链接：https://arxiv.org/abs/2601.05746
- 类型：multi-agent debate 改进

主要内容：

DynaDebate 研究 multi-agent debate 中的 homogeneity 问题。传统 MAD 方法常常让多个 agent 从类似的初始路径出发，导致它们犯同样的错；最后所谓 debate 退化成简单多数投票。

做了什么：

- 提出 Dynamic Multi-Agent Debate。
- 包含三个机制：
  - Dynamic Path Generation and Allocation：专门的 Path Generation Agent 生成多样且逻辑合理的解题路径。
  - Process-Centric Debate：从争最终答案转向逐步批判推理过程。
  - Trigger-Based Verification Agent：当出现分歧时，用验证 agent 和外部工具解决僵局。

最后结论：

DynaDebate 在多个 benchmark 上优于已有 MAD 方法。论文的核心结论是：有效 debate 不应该是多个 agent 给答案后投票，而应该保证路径多样性、过程批判和工具验证。

对 DAClaw 的启发：

- 如果 DAClaw 引入 multi-agent，最好不要做“多个 subagent 同时回答然后投票”。
- 更适合：
  - 让不同 agent 生成不同计划或执行路径。
  - 让 verifier 批判过程，而不是只选最终答案。
  - 当结论冲突时调用工具验证。
- 对数据分析任务，可以使用双路径复算：

```text
Pandas path
DuckDB/SQL path
Verifier compares outputs
```

### 4. ToolWeaver: Weaving Collaborative Semantics for Scalable Tool Use in Large Language Models

- 时间：2026-01-29
- 链接：https://arxiv.org/abs/2601.21947
- 类型：大规模工具使用与工具语义学习

主要内容：

ToolWeaver 研究大规模工具库下的工具选择问题。传统 retrieval-based tool-use pipeline 有双重语义问题：retriever 不一定理解复杂语义，LLM 本身也缺少天然工具知识。把每个工具映射成独立 token 又会造成词表膨胀和语义隔离。

做了什么：

- 提出 ToolWeaver，一个 generative tool learning framework。
- 把工具编码成 hierarchical sequences。
- 词表扩展随工具数量对数增长。
- 通过 structured tokenization 将工具的 intrinsic semantics 和 extrinsic co-usage patterns 结合。
- 在 generative alignment 阶段 fine-tune LLM 生成工具层级编码。
- 在接近 47,000 个工具的设置下评估。

最后结论：

ToolWeaver 显著优于已有方法，说明 advanced tool-augmented agents 需要更 scalable、generalizable、semantically-aware 的工具表示，而不是简单工具名检索。

对 DAClaw 的启发：

- DAClaw 的工具越来越多时，单靠 toolset 和 schema description 会遇到瓶颈。
- 可以考虑在 framework 层维护 tool semantics / tool co-usage graph。
- 对 Claw 类任务，工具不是孤立调用：浏览器、文件、OCR、表单、数据工具之间有协作关系。
- 这支持“workflow pattern / tool-use skill”作为 DAClaw 的长期增强。

## 边界外但值得补充的相关论文

以下论文略早于严格半年窗口，但和你当前思路高度相关，可以作为背景参考，不放入主体清单。

### A. Stronger-MAS: Multi-Agent Reinforcement Learning for Collaborative LLMs

- 时间：2025-10-13，2026-01-29 修订
- 链接：https://arxiv.org/abs/2510.11062
- 类型：multi-agent RL / collaborative LLMs

主要内容：

论文研究如何把 on-policy RL 用到 multi-agent LLM systems。它指出传统 GRPO 分组假设在 MAS 中会失效，因为不同 agent、不同轮次的 prompt 不同。

做了什么：

- 提出 AT-GRPO。
- 按 agent 和 turn 做 grouped RL。
- 支持 single-policy 和 multi-policy MAS。
- 在 game、planning、coding、math 上评估。

最后结论：

在 long-horizon planning 中，准确率从 14.0%-47.0% 提升到 96.0%-99.5%；coding 和 math 也有明显增益。结论是：multi-agent 的协作策略可以被训练，而不是只靠 prompt。

对 DAClaw 的启发：

- 现阶段不必训练 RL，但应该先记录可训练的 trajectory。
- 如果未来做 RL 或 offline improvement，DAClaw 需要结构化记录 agent/turn/tool/action/reward。

### B. MAS^2: Self-Generative, Self-Configuring, Self-Rectifying Multi-Agent Systems

- 时间：2025-09-29
- 链接：https://arxiv.org/abs/2509.24323
- 类型：自动配置和自修正 multi-agent system

主要内容：

MAS^2 认为很多自动 MAS 属于 generate-once-and-deploy，面对动态任务很脆弱。它提出 recursive self-generation，让一个 MAS 自动构建适合当前问题的目标 MAS。

做了什么：

- 设计 generator-implementer-rectifier 三 agent 团队。
- 动态组合和修正目标 agent system。
- 用 Collaborative Tree Optimization 训练这些 meta-agents。
- 在 7 个 benchmark 上评估。

最后结论：

MAS^2 在复杂 deep research 和 code generation 上相对 SOTA MAS 提升最高 19.6%，并有较好的跨模型泛化。结论是：agent system 的配置本身可以成为动态生成和修正对象。

对 DAClaw 的启发：

- DAClaw 不需要直接实现完整 MAS^2。
- 但可以实现轻量版 self-configuring scaffold：

```text
task type -> choose skill pack / context pack / verifier / repair policy
```

### C. MCPMark: A Benchmark for Stress-Testing Realistic and Comprehensive MCP Use

- 时间：2025-09-28
- 链接：https://arxiv.org/abs/2509.24002
- 类型：真实 MCP 工具使用 benchmark

主要内容：

MCPMark 虽然略早于半年窗口，但非常适合作为 Claw 系任务的旁证。它关注真实 MCP 工具环境中的长流程 CRUD 操作。

做了什么：

- 构建 127 个高质量任务。
- 每个任务有 curated initial state 和 programmatic verification script。
- 强调 create/read/update/delete，而不是只读任务。
- 使用最小 agent tool-calling loop 测试 frontier models。

最后结论：

最佳模型 gpt-5-medium 也只有 52.56% pass@1 和 33.86% pass^4。平均每个任务需要 16.2 个 execution turns 和 17.4 次 tool calls，说明真实工具任务的难点在长流程、状态管理和副作用控制。

对 DAClaw 的启发：

- Claw 系任务和 MCP 工具任务有共同瓶颈：状态追踪、写操作、长流程验证。
- DAClaw 的 framework 改进应该优先关注 action trace、state snapshot、write guard、repair，而不是只扩展工具数量。

## 对 DAClaw 的统一启发

从这些论文看，DAClaw 可以考虑的“现象级改进”不是新建一个庞大的 multi-agent 架构，而是在 general agent 内加入一个更强的认知 scaffold：

```text
Main Agent
  -> Context Profiler
  -> TaskState Builder
  -> EvidenceGraph Updater
  -> ActionGuard
  -> Verifier / Repair Agent
  -> Workflow Distiller
```

### 可以抽象出的四个核心对象

#### 1. TaskState

用于记录当前任务目标、实体、约束、未解决问题、待执行动作。

适合 ClawBench 的网页流程、ClawsBench 的办公任务、DAComp 的数据分析任务、ResearchRubrics 的研究任务。

#### 2. EvidenceGraph

用于记录每个结论、字段、表单值、统计结果、网页信息来自哪里。

这能把数据分析里的 evidence-grounding 扩展到 general agent。

#### 3. ActionGuard

用于在写操作前做检查。

适合提交表单、发送邮件、修改文档、创建日历、执行代码 patch 等高风险动作。

#### 4. WorkflowMemory

用于从成功或失败轨迹中抽取可复用 workflow。

这和 Hermes 的 skill learning 类似，但不是只保存自然语言 skill，而是保存：

```text
trigger
steps
required evidence
common failures
validation rules
repair strategies
```

## 推荐阅读顺序

如果你现在的目标是找 DAClaw 的改进方向，建议按这个顺序读：

1. **ClawsBench**：看 OpenClaw-style agent scaffold 到底哪里影响能力和安全。
2. **ClawBench**：看真实日常网页任务到底难在哪里。
3. **Claw-Eval**：看为什么轨迹、审计、安全和鲁棒比最终答案更重要。
4. **DoVer**：看失败后如何用 intervention 做可验证修复。
5. **M2CL**：看 multi-agent 的关键为什么是上下文对齐。
6. **DynaDebate**：看多路径、过程批判、工具验证如何替代简单投票。
7. **DAComp**：看数据分析能力如何抽象为 general task scaffold。
8. **ResearchRubrics**：看开放式任务如何用 evidence 和 rubrics 评估。
9. **ToolWeaver**：看工具很多时，为什么需要工具语义和协作模式。

## 一句话总结

最近半年相关工作给 DAClaw 的核心启发是：

> 未来强 general agent 的改进点不是“更多 subagent”，而是“更好的结构化状态、更可靠的证据追踪、更安全的写操作、更可验证的失败修复、更可沉淀的 workflow memory”。Multi-agent 在这里更适合做 context profiling、verification、repair、distillation，而不是让多个 agent 自由讨论或投票。
