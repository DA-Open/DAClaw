# DAClaw Repository Cleanup And Software Roadmap

## 中文速读

你现在遇到的 Web 不稳定，核心原因不是某一个按钮写坏了，而是：

1. Web 前端已经像软件界面，但后端仍然是临时 dashboard 风格。
2. 前端会把对话、trace、部分大内容放进浏览器状态或 `localStorage`。
3. `WorkspacePage`、`ChatPage` 等页面较重，首屏加载成本高。
4. `daclaw_cli/web_server.py` 把太多 API 和业务逻辑塞在一个文件里。
5. 核心运行时仍然围绕 `run_agent.py` 的单体主循环，没有独立的 Run/Event/Artifact 层。

如果最终要做成软件，建议路线不是立刻重写，而是：

```text
先稳定 Web
  -> 再拆 Web 后端
  -> 再抽 Runtime 的 Run/Event/Artifact
  -> 再做 DataAgent 专用 workflow
  -> 最后用 Tauri/Electron 包成桌面软件
```

最先做的 5 件事：

1. 修复中文乱码。
2. Web 路由懒加载，减少打开时间。
3. 浏览器只存轻量状态，不存大对话和大 trace。
4. 新增 Web UI cache reset。
5. 把 `web_server.py` 的 workspace preview/chat/config 路由逐步拆成模块。

This roadmap focuses on making the project understandable, making the Web UI
stable, and preparing DAClaw to become a desktop software product.

## Current Pain Points

### 1. Startup Feels Slow

The dashboard currently starts through `daclaw dashboard`, which can build or
check the Vite frontend, import a large Python application, start FastAPI, and
serve a single-page app. For a desktop-style product, this feels slow because
the user expects a native app to open immediately.

Short-term fix:

- Use prebuilt `daclaw_cli/web_dist` for normal launches.
- Only run `npm install` / `npm run build` in explicit development commands.
- Add a lightweight health endpoint and show a loading shell while the Python
  backend warms up.

Long-term fix:

- Package the frontend as a desktop shell.
- Start the Python backend as a managed child process.
- Persist backend status and logs separately from the browser UI.

### 2. Browser Cache / Storage Can Freeze The Page

The Web chat currently keeps local conversation copies in `localStorage`. It
does truncate content and traces, but the browser is still the wrong place to
own large agent transcripts, tool outputs, generated reports, and analysis
artifacts.

Short-term fix:

- Store only lightweight UI pointers in `localStorage`: current conversation id,
  selected workspace path, last active tab.
- Move full chat messages, trace events, and run outputs to backend storage.
- Add a "clear Web UI cache" command/button that only clears browser UI state,
  not DAClaw sessions.

Long-term fix:

- Introduce `RunStore` under `DACLAW_HOME/runs/`.
- Let the frontend load paginated run events from `/api/runs/{id}/events`.
- Use virtualized rendering for long traces and large tables.

### 3. One Big Frontend Bundle

The frontend currently builds into one large JavaScript chunk. This increases
first open time and makes every page pay for code that only some pages need.

Short-term fix:

- Lazy-load routes with `React.lazy`.
- Split heavy pages: `WorkspacePage`, `ChatPage`, `ConfigPage`, `LogsPage`.
- Defer Markdown/KaTeX-heavy rendering until chat/report pages are opened.

### 4. One Big Web Backend File

`daclaw_cli/web_server.py` mixes configuration APIs, OAuth APIs, sessions,
workspace file browsing, preview logic, chat streaming, static serving, and
server startup.

Suggested split:

```text
daclaw_cli/web/
  __init__.py
  app.py              # create FastAPI app and middleware
  auth.py             # session token, rate limits
  server.py           # start_server()
  routes/
    config.py
    env.py
    oauth.py
    sessions.py
    workspace.py
    chat.py
    logs.py
    cron.py
    skills.py
  services/
    workspace.py
    chat_runs.py
    previews.py
```

Keep `daclaw_cli/web_server.py` temporarily as a compatibility shim:

```python
from daclaw_cli.web.server import start_server
from daclaw_cli.web.app import app
```

### 5. Core Agent Runtime Is Too Centralized

`run_agent.py` still owns too many concerns: conversation loop, provider
handling, prompt setup, tool orchestration integration, context repair,
analysis audit, display callbacks, session logging, and more.

Do not rewrite it all at once. Extract these in order:

1. `agent/runtime/events.py`
2. `agent/runtime/run_store.py`
3. `agent/runtime/artifacts.py`
4. `agent/runtime/modes.py`
5. `agent/data_agent/workflow.py`

This lets Web, CLI, Gateway, and future desktop app share the same event and
artifact model.

## Recommended Desktop App Direction

Because the current UI is React and the backend is Python, the least disruptive
path is:

```text
Desktop shell: Tauri or Electron
Frontend: existing web/ React app
Backend: Python FastAPI child process
State: DACLAW_HOME profile directories
```

Recommended first choice: Tauri, if the team is comfortable with Rust tooling.
It gives a lighter desktop shell and can still call a local Python process.

Fallback choice: Electron, if fast iteration matters more than app size.

Avoid rewriting the UI in PySide/Tk right now. The React UI already exists, and
rewriting it would delay the architecture cleanup without fixing the runtime
problems.

## Cleanup Phases

### Phase 0: Make The Repository Understandable

Goal: no architecture changes yet.

- Keep `docs/current-architecture-map.md` updated.
- Fix encoding damage in Chinese docs and UI strings.
- Add a short `docs/entrypoints.md` if new entry points are added.
- Remove or archive empty legacy folders such as `hermes_cli/` only after
  confirming no tooling expects them.

### Phase 1: Stabilize Web

Goal: make dashboard reliable before turning it into software.

- Add route-level code splitting.
- Move large chat/run state out of `localStorage`.
- Add backend-owned run event storage.
- Add frontend cache reset.
- Add pagination/virtualization for trace and file preview views.
- Put hard response limits on preview endpoints.

### Phase 2: Split Web Backend

Goal: make Web backend maintainable.

- Extract `daclaw_cli/web_server.py` into package modules.
- Keep API routes behavior-compatible.
- Add route-level tests for `/api/workspace/*`, `/api/chat/*`, and `/api/config/*`.

### Phase 3: Introduce Runtime Concepts

Goal: prepare for a real DAClaw architecture.

Add:

```text
agent/runtime/events.py
agent/runtime/run_store.py
agent/runtime/artifacts.py
agent/runtime/modes.py
```

Then make Web chat consume runtime events instead of custom callback wiring.

### Phase 4: Build DataAgent Runtime

Goal: turn the Data Studio UI into a real data-analysis system.

Add:

```text
agent/data_agent/
  workflow.py
  artifacts.py
  validation.py
  provenance.py
  reports.py
```

Define a lifecycle:

```text
intake -> profile -> plan -> execute -> validate -> report -> audit -> persist
```

### Phase 5: Desktop Packaging

Goal: software experience.

- Bundle prebuilt frontend.
- Spawn backend as child process.
- Show app shell immediately.
- Add backend startup progress.
- Add one-click cache reset and logs.
- Add update/install flow.

## What Not To Do First

- Do not start by moving every folder into a new structure.
- Do not rewrite `run_agent.py` from scratch.
- Do not make desktop packaging the first fix for Web instability.
- Do not let Web, CLI, and Gateway each invent separate task state models.
- Do not store large reports, traces, or data previews in browser storage.

## First Practical Sprint

The most useful first sprint is small:

1. Fix Chinese encoding damage in `web/src/i18n/zh.ts`, `web/src/App.tsx`, and
   docs where visible.
2. Add route-level lazy loading in `web/src/App.tsx`.
3. Add a Web UI cache reset control.
4. Stop persisting full assistant content in `localStorage`; persist only ids and
   lightweight summaries.
5. Extract workspace preview helpers from `daclaw_cli/web_server.py` into a
   service module.

After this sprint, DAClaw should feel more stable and the repository should be
easier to modify without fear.
