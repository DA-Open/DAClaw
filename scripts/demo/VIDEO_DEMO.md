# DAClaw — Video Demo Script (NovaRetail)

> 用于 2 分半演示视频。数据库、用例都已**端到端验证通过**。用例正文全英文,✅ 后是给你的中文操作指导。

---

## 0. 录制前准备(务必按顺序)

**① 数据库**:`~/Downloads/novaretail.sqlite`(NovaRetail 全渠道零售 + 订阅 SaaS,12 张表:customers 800 / orders 9K / order_items 27K / products 180 / subscriptions 397 / marketing_spend / web_events 15K / returns 1.7K / inventory / suppliers / categories / stores;含黑五季节性、促销折扣、流失、退货、脏数据)。

**② 保证全英文**:右上角语言切换成 **EN**。若刷新后又变中文,在浏览器控制台执行 `localStorage.setItem('daclaw-locale','en')` 再刷新(中文系统会按浏览器语言自动切成 zh,所以要显式钉成 en)。

**③ 清空历史**:在浏览器控制台粘贴这一段后刷新(清掉对话/ETL/合成历史,并钉住英文):
```js
['daclaw.dataAgent.v2','daclaw.etl.history.v1','daclaw.synth.history.v1','daclaw.dataAgent.preselect'].forEach(k=>localStorage.removeItem(k));
localStorage.setItem('daclaw-locale','en');
location.reload();
```
(服务器端生成的图表/视频/音频产物我已清空。)

**④ 连接数据库**:左栏「Connect database」→ 选 `~/Downloads/novaretail.sqlite` → 左侧应出现 12 张表。

**⑤ 录制顺序**:先做 ①②③(Data Analysis),**dbt 管线 ④ 放最后**——因为 ④ 会往引擎里加 14 张 model 表,会让左栏变乱。录完 ④ 若要重来,重启后端并只连 NovaRetail 即可。

---

## Case 1 — Agentic analysis: SQL + chart + insight
**Board: Data Analysis**

> Analyze our Black Friday performance: compare total net revenue (after discounts and returns) during the Black Friday window (Nov 22–30) versus the rest of the year, broken down by product department, and visualize it as a chart.

✅ **操作**:先点输入框旁的 **「Compile plan」**→ 弹出计划 DAG,点其中任意节点展示「节点检查器(输入 / 执行的 SQL / 输出)」→ 点 **「Approve & Run」**。跑完右侧出现按部门的净收入柱状图,回答里会精确定义 net revenue。**卖点**:自主 agent、人审批的计划门、可解释的节点级 SQL。

---

## Case 2 — Real parallel execution
**Board: Data Analysis**

> Run these three independent analyses in parallel: (1) top 5 countries by net revenue, (2) monthly active subscription MRR trend, (3) the web funnel conversion rate from product_view to purchase. Use the parallel query tool.

✅ **操作**:直接发送即可。轨迹里会出现 `data_parallel_query`(`ran_in_parallel: true`),三条互不依赖的分支并发执行。**卖点**:真并行 DAG 执行,不是串行。

---

## Case 3 — Narrated data video
**Board: Data Analysis**

> Create a narrated executive data video (data_story with narrate=true) with 4 slides telling the story of our 2025 growth: an opening title, a chart of quarterly net revenue, a KPI slide with total customers/orders/MRR, and a closing recommendations slide. Give each slide a one-sentence spoken narration in English.

✅ **操作**:发送后右侧生成数据视频,点 **▶ Play** —— 会逐页**英文语音旁白**并随旁白结束自动翻页。**卖点**:带旁白的"数据视频",很适合做视频里的炫酷镜头(建议留 15–20 秒给它播放)。

---

## Case 4 — Auto-DBT analytics warehouse (放最后录)
**Board: Data Engineering**

> Build a customer analytics warehouse: stage customers, orders, order_items and returns; build intermediate models for order economics (net revenue per order after discounts and refunds) and customer activity; then marts for customer lifetime value, monthly revenue by department, and a churn-risk table joining subscriptions with recent activity. Reuse shared intermediates across branches.

✅ **操作**:在「Data Engineering」页粘贴 → 「Build pipeline」。生成 **14/14 models、22 节点、6 层深**的分叉血缘 DAG。点任意 model 节点 → 检查器显示 SQL/预览 → 「Analyze here」可就地追问。**卖点**:自然语言 → 分层 dbt 管线 + 可视化血缘图。

---

## (可选)结尾 15 秒 — Data Synthesis
**Board: Research → Data Synthesis**

Research table 模式,spec 填:

> A clinical trial patient cohort: demographics, treatment arm, baseline vitals, outcomes.

✅ **操作**:选 count、格式(jsonl/xlsx)→ Synthesize → 秒出可下载的合成表格。**卖点**:面向科研的数据合成(训练数据 + 任意风格表格,含 S3Eval 风格的无泄漏 SQL 评测数据)。

---

## 建议叙事线(2:30)
1. **0:00–0:20** 开场 + 连接 NovaRetail(12 表,真实业务)。
2. **0:20–0:55** Case 1:提问 → 计划门 → 节点 SQL → 图表 + 洞察。
3. **0:55–1:20** Case 2:一句话触发三路并行分析。
4. **1:20–1:50** Case 3:生成并播放带旁白的数据视频。
5. **1:50–2:20** Case 4:自然语言一键生成 dbt 数仓 + 血缘 DAG。
6. **2:20–2:30** Data Synthesis 秒出科研数据集,收尾。
