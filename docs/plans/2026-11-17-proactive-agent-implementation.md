# Proactive Agent 初步实现与验收（2026-11-17）

实现依据为《Proactive框架设计规划_2026-11-17.md》，本次范围是 Q1–Q3：
可信订阅/事件 → P 门 → 后台任务 → 数据卡及统计重放核验 → D 门 → 站内待看区 → Main 按需读取。
飞书外部通知（Q4）、真实模型效果和 DAComp 增益评测（Q5）不在本次完成范围。

## 分支与依赖

- 分支：`feat/proactive-agent`。
- 基于 P0 集成分支 #22；移植 #23 的类型提交，再单独提交规划要求的契约调整。
- 已合入 `DaraOS` 的 `d1a5591c`（#25）。创建本分支时 #22、#23 仍未合并，
  因此对 `DaraOS` 的 PR 也包含这些前置改动；审阅 Proactive 时应从契约调整提交开始。
- 未改变旧 hermes agent loop。旧 `DataEngine` 仅增加关闭自动恢复数据源的选项、
  SQLite 只读打开方式及标识符转义；新 Connector 复用其统计/查询函数。

## 已实现范围

| 阶段 | 实现 | 边界 |
|---|---|---|
| Q1 订阅与事件 | token 身份绑定 scope；订阅资源是用户白名单的子集；事件去重；每订阅/事件一条决策；准入与建任务同一事务 | 不自动订阅；不实现 grants 表；外部来源签名验证留到接入对应来源时 |
| Q1 P 门与资源 | `skip/defer/admit`、baseline/diff、订阅日用量、scope 容量、较小单任务预算 | 日用量按 UTC 已结算用量做准入，任务自身预算独立执行；不是日额度的硬预留 |
| Q1 Scheduler | 五字段 UTC cron、持久化 defer 重评、只读 Proactive 过期租约回收 | 需先收到数据源事件；恢复只覆盖 `proactive@1` + `data_card@1`，不覆盖通用 P1 恢复 |
| Q2 数据卡 | 独立 Proactive 上下文；`data.profile`、`data.query_readonly`；`data_card@1`；统计绑定 action 并重放核对 | 仅复用本地文件数据源；计算字段不能由模型编造；语义推断只允许 grain/meaning/unit 并附置信度 |
| Q2 隔离 | 角色/用户/能力工具交集 + 订阅资源约束；只读数据源；查询最多 1,000 行、30 秒；前后台分 Worker | `artifacts.write` 只写本任务产物；不开放外部写工具；生产应配置共享、可读取的数据文件与产物目录 |
| Q3 待看区 | `2R+I+U`、三档主动程度、UTC 静默时段、有效窗口、原子发布 inbox 与 ready 事件 | `notify` 是站内优先级，不发送飞书消息；当前质量发现是空值、候选键重复和空表，不能等同于业务缺陷 |
| Q3 Main 交接 | 关联数据源/当前及最近三条请求的表列关键词/订阅关注点；最多三条短指针；读取前验证权限、版本、核验与时效 | 使用 `document_draft@2` 启用后台读取；保持 `document_draft@1` 契约；不在流式输出途中注入 |
| Q3 去重与反馈 | Main 读取前预留共享通知键；重复读取不重复顺带提及；站内反馈写回决策；JSONL 导出含 defer 历史 | 用户明确追问可读取标注版本/有效期的旧结果，但仍受当前权限限制；外部通知配额与对账留到 Q4 |

## 本地启动

从仓库根目录运行，使用 Python 3.11+，安装 `harness` 与 `data` extras。
下列示例为 Linux/WSL；API、Scheduler、Worker 使用同一 PostgreSQL、权限文件和绝对产物路径。

```bash
python -m pip install -e '.[harness,data]'
export PYTHONPATH=src
export DARAOS_HARNESS_DATABASE_DSN='postgresql://<user>:<password>@<host>:5432/<database>'
export DARAOS_HARNESS_ARTIFACT_ROOT=/srv/daraos/artifacts
export DARAOS_HARNESS_POLICY_FILE=/srv/daraos/policy.yaml
export DARAOS_HARNESS_BACKGROUND_SCOPE_LIMIT=2
python -m harness.storage.migrate upgrade
```

权限文件示例（令牌仅存 SHA-256；填入实际摘要，勿把原令牌提交到仓库）：

```yaml
tokens:
  - sha256: <token-sha256>
    scope_id: demo
    actor_id: demo-user
users:
  demo:
    main:
      paths: [/srv/daraos/data]
    proactive:
      paths: [/srv/daraos/data]
disabled_tools: []
```

分别启动四个进程；模型配置格式沿用 [Harness README](../../src/harness/README.md)：

```bash
python -m harness.api --port 8090
python -m harness.scheduler
python -m harness.worker --lanes foreground --model-config /srv/daraos/model.yaml
python -m harness.worker --lanes background --model-config /srv/daraos/model.yaml
```

API 和 Scheduler 不调用模型。真实模型 Worker 尚未作为本次验收依据。
多个 API/Worker 进程必须能访问相同的绝对数据文件路径；仅共享 PostgreSQL 不会共享文件。

## API 使用顺序

请求均带 `Authorization: Bearer <token>`。订阅和事件的 `subject_ref` 使用同一绝对路径。

1. `POST /subscriptions` 创建订阅：

```json
{
  "goal": "了解销售数据结构并检查一般数据质量",
  "subject_ref": "/srv/daraos/data/sales.sqlite",
  "resources": ["/srv/daraos/data/sales.sqlite"],
  "event_filter": {"source": "dataset"},
  "proactivity": "normal",
  "result_ttl_sec": 86400,
  "focus": ["sales", "amount"]
}
```

2. `POST /events` 发送数据源版本事件：

```json
{
  "source": "dataset",
  "source_event_id": "sales-v1",
  "subject_ref": "/srv/daraos/data/sales.sqlite",
  "data": {"kind": "data_connected", "data_version": "v1", "fingerprint": "<content-fingerprint>"}
}
```

返回已准入任务列表；同一 `source + source_event_id + scope` 重发不重复建任务。
更新时使用新的事件 ID、`kind=data_updated`、版本和数据指纹。
Scheduler 的 `schedule` 事件不能从该 API 伪造。

3. `GET /tasks/{task_id}`、`GET /tasks/{task_id}/trace` 查看执行及核验；
   `GET /inbox` 查看当前可用数据卡。`notify` 仍是站内待看项。
4. Main `POST /tasks` 选择 `capability_ref=document_draft@2` 并关联该数据源或提到相关表列。
   ContextBuilder 只注入主题/重要度/ID，模型通过 `background_results.read` 按需读取。
5. `POST /inbox/{id}/ack` 提交 `{"status":"handled","label":"useful"}`。
   状态还可为 `read/ignored`，标签可为 `not_useful/harmful`。
6. `GET /subscriptions/{id}/decisions` 导出 JSONL；
   `DELETE /subscriptions/{id}` 撤销并取消该订阅未结束任务及未发待看项。

静默时段是 `[start_hour, end_hour)`，支持跨午夜，`[0,0]` 表示不设置静默时段。
cron 只生成最近一次到期触发，不追补所有错过的时刻；defer 事件会持久重评。
`diff` 任务携带旧版本/上次数据卡，仍输出当前版本的数据卡，不额外生成未经核验的差异数字。

## 可复现的无付费模型演示

```bash
PYTHONPATH=src python scripts/harness/proactive_data_card_demo.py \
  --database /data/dacomp-004.sqlite --output /tmp/proactive-demo
```

脚本使用确定性三步 adapter，但事件准入、DataEngine、Gateway action 记录、
Verifier 重放、Worker 提交、inbox、Main 指针与反馈都经过真实实现。
输出 `data-card.json` 和 `acceptance.json`。adapter 从持久化工具结果构造数据卡，
不做语义推断，也不证明真实 LLM 能稳定生成数据卡。

2026-11-17 在 DAComp `dacomp-004` SQLite 数据库完成一次端到端验收：
任务 `succeeded`，5 项核验均 `passed`，1 张表、0 条当前规则发现、1 条 Main 指针，
反馈 `useful` 已写回。没有调用付费模型或发送外部消息。

## 验证

本实现用同一组场景覆盖内存和 PostgreSQL：事件并发去重、撤销/缓存权限、defer/次日预算、
scope 并发与前台独立、cron 重启、过期租约、统计篡改、SQL/路径边界、过期/旧版本、
短指针和去重反馈。PostgreSQL 故障注入验证了两处事务回滚：准入时决策写入失败不能留下任务；
完成时 inbox 写入失败不能留下成功状态或 ready 事件。

完整 Harness 测试命令（包含数据库测试；测试夹具可启动临时 PostgreSQL）：

```bash
python -m pytest tests/harness_runtime -o addopts= -q
```

本次完整 Harness 回归：**485 passed，0 failed/errors/skipped**；`uv lock --check` 通过。
固定实现提交 `4052c913` 的全仓检查为 **12,523 passed / 40 failed / 10 errors / 36 skipped**；
失败/错误全部位于 Harness 之外的 CLI/Gateway 等模块，未将全仓声明为通过。
从全仓失败中提取的 47 个非 Harness 用例在未加入 Proactive 实现的基线 `376ffcfe` 上
复现 26 项失败；本分支同组重跑 27 项失败，其中差异为并发批准/拒绝用例。
全仓记录及环境限制在 PR 中列出，未为消除无关失败修改业务代码或放宽断言。
验证环境使用 Linux 本地虚拟环境；位于 Windows 挂载盘的虚拟环境启动较慢，
会触发现有进程测试的启动超时，未因此放宽断言。

## 后续工作

Q4 需要受控 outbox、渠道回执、通知额度与 unknown 对账后才能接飞书主动通知。
Q5 需要问题注入/安静对照、真实模型生成稳定性，以及同批 DAComp 题有无数据卡的成本与质量比较。
当前 D 门分数和质量规则是初值，尚无 precision/recall、打扰率或 Main 增益结论。
