# DAClaw Current Architecture Map

## 中文速读

现在的 DAClaw 可以先这样理解：

```text
多个入口界面
  -> 同一个 AIAgent 主循环
  -> model_tools 分发工具调用
  -> tools/registry.py 找到具体工具
  -> tools/*.py 执行工具
```

所以你现在看到的 Web、CLI、Gateway、ACP 并不是四套独立系统，而是四个入口共同围绕
`run_agent.py` 里的 `AIAgent.run_conversation()` 工作。

当前最重要的几个文件是：

- `run_agent.py`：核心大脑，也是最难改的大文件。
- `model_tools.py`：模型发起 tool call 以后，从这里进入具体工具。
- `tools/registry.py`：所有工具注册和分发的中心。
- `toolsets.py`：决定某种模式下有哪些工具可用。
- `daclaw_cli/main.py`：`daclaw` 命令入口。
- `daclaw_cli/web_server.py`：本地 Web 后端，目前过大，需要拆。
- `web/src/App.tsx`：Web 前端路由和外壳。
- `web/src/pages/WorkspacePage.tsx`：数据工作台页面。
- `web/src/pages/ChatPage.tsx`：完整聊天页面。

一句话判断：现在是“继承 Hermes/OpenClaw 的通用 Agent 架构，加了 DAClaw Web
工作台和初步 DataAgent 能力”，还不是一个从底层重新设计的 DAClaw 软件架构。

This document is a practical map of the current DAClaw repository. It is meant
for maintainers who want to understand the inherited Hermes/OpenClaw-style
architecture before refactoring it.

## Mental Model

DAClaw is currently a general agent runtime with several user interfaces around
one central synchronous agent loop.

```text
CLI / Web / Gateway / ACP / Batch / Cron
        |
        v
run_agent.AIAgent
        |
        v
model provider call -> tool call -> model_tools.handle_function_call()
        |
        v
tools.registry -> tools/*.py handlers
```

The important implication: most product surfaces still depend on the same
`AIAgent.run_conversation()` loop. The Web UI is a dashboard and chat surface,
but it is not yet a separate Web-native runtime.

## Top-Level Files

| Path | Role | Notes |
| --- | --- | --- |
| `run_agent.py` | Core agent runtime | Defines `AIAgent`; owns the main conversation loop, prompt setup, tool-call loop, context handling, and final response flow. This is the biggest inherited core file. |
| `model_tools.py` | Tool discovery and dispatch bridge | Imports tool modules, collects schemas, resolves enabled tools, and dispatches model tool calls to handlers. |
| `toolsets.py` | Tool grouping | Defines toolsets such as `web`, `data`, `terminal`, `browser`, and the shared core tool list. |
| `cli.py` | Interactive terminal UI | Prompt toolkit/Rich CLI interface. Large legacy file; many UI concerns and command flows live here. |
| `daclaw_state.py` | SQLite session store | Conversation/session persistence and search support. |
| `daclaw_constants.py` | Profile-safe paths | `get_daclaw_home()` and `display_daclaw_home()` live here. Use these instead of hardcoded `~/.daclaw`. |
| `pyproject.toml` | Packaging metadata | Python package, extras, console scripts, package data, pytest defaults. |

## Main Packages

### `agent/`

Agent internals that support `run_agent.py`.

Important files:

| Path | Role |
| --- | --- |
| `agent/prompt_builder.py` | Builds reusable system prompt sections. |
| `agent/context_compressor.py` | Context compression when history grows too large. |
| `agent/prompt_caching.py` | Anthropic prompt caching support. |
| `agent/auxiliary_client.py` | Secondary LLM calls for summarization, vision, audits, etc. |
| `agent/model_metadata.py` and `agent/models_dev.py` | Model context/capability lookup. |
| `agent/data_sources.py` | Early DataAgent source profiling abstraction. Currently CSV/TSV focused. |
| `agent/data_catalog.py` | Profile-scoped structured data catalog. |
| `agent/analysis_audit.py` | LLM-driven audit for data-analysis report faithfulness. |

Current state: `agent/` contains useful support modules, but the actual runtime
boundary is still centered on `run_agent.AIAgent`.

### `tools/`

Tool implementations. Each tool file registers itself with `tools.registry`.

Important files:

| Path | Role |
| --- | --- |
| `tools/registry.py` | Central registry for schemas, handlers, toolsets, availability checks. |
| `tools/terminal_tool.py` | Terminal/process execution backend. |
| `tools/file_tools.py` | Read/write/search/patch file tools. |
| `tools/web_tools.py` | Web search and extraction. |
| `tools/browser_tool.py` | Browser automation. |
| `tools/code_execution_tool.py` | Code execution/sandbox support. |
| `tools/delegate_tool.py` | Subagent delegation. |
| `tools/data_analysis_tool.py` | `data_profile` tool. |
| `tools/data_catalog_tool.py` | `data_catalog` tool. |
| `tools/analysis_audit_tool.py` | Manual `analysis_audit` tool. |
| `tools/environments/` | Local, Docker, SSH, Modal, Daytona, Singularity backends. |

Tool flow:

```text
tools/*.py calls registry.register(...)
        |
model_tools.discover_builtin_tools()
        |
run_agent passes schemas to model
        |
model_tools.handle_function_call()
        |
registry dispatches handler
```

### `daclaw_cli/`

Command-line application package and local Web dashboard backend.

Important files:

| Path | Role |
| --- | --- |
| `daclaw_cli/main.py` | `daclaw` console entry point and subcommand parser. |
| `daclaw_cli/config.py` | Default config, env var metadata, config migration helpers. |
| `daclaw_cli/commands.py` | Slash command registry shared by CLI/gateway. |
| `daclaw_cli/web_server.py` | FastAPI backend for the local Web dashboard. Currently large and monolithic. |
| `daclaw_cli/auth.py` and `auth_commands.py` | Provider credential and OAuth flows. |
| `daclaw_cli/skin_engine.py` | CLI theme/skin system. |
| `daclaw_cli/tools_config.py` and `skills_config.py` | Interactive configuration UIs. |

Dashboard flow:

```text
daclaw dashboard
        |
daclaw_cli.main.cmd_dashboard()
        |
build web/ with npm if needed
        |
daclaw_cli.web_server.start_server()
        |
FastAPI serves web_dist and /api/*
```

### `web/`

React/Vite frontend for the local dashboard.

Important files:

| Path | Role |
| --- | --- |
| `web/src/App.tsx` | App shell, routing, top navigation. |
| `web/src/pages/WorkspacePage.tsx` | Data workspace UI: files, preview, workflow/report/dashboard panels, assistant. |
| `web/src/pages/ChatPage.tsx` | Full chat UI with tool trace panel. |
| `web/src/lib/api.ts` | Frontend API client. |
| `web/src/i18n/` | English/Chinese translations. Some Chinese strings currently show encoding damage. |

Current stability risks:

- The app is bundled mostly as one large route tree.
- `ChatPage` and `WorkspacePage` keep conversation state in `localStorage`.
- Large assistant responses and trace events can pressure browser storage and
  rendering.
- Workspace previews can return large HTML/text/database previews.
- The backend has in-memory chat run state inside one FastAPI process.

### `gateway/`

Messaging platform gateway. It adapts Telegram, Discord, Slack, WhatsApp,
Signal, etc. into agent turns.

Important files:

| Path | Role |
| --- | --- |
| `gateway/run.py` | Main gateway loop and slash command dispatch. |
| `gateway/session.py` | Messaging session persistence. |
| `gateway/platforms/` | Platform adapters. |
| `gateway/status.py` | Runtime status and scoped platform locks. |

Gateway should remain an adapter layer. DataAgent or Web-specific logic should
not be duplicated here.

### `acp_adapter/`

ACP server integration for editor clients such as VS Code/Zed/JetBrains-style
frontends.

This is another adapter around the same core runtime.

### `cron/`

Scheduled agent jobs. `cron/jobs.py` and `cron/scheduler.py` store and execute
natural-language scheduled tasks.

### `environments/`

Training, benchmark, and evaluation environments. This is useful for future
DataAgent evaluation because it already has the idea of agent loops, tasks,
trajectories, and rewards.

### `plugins/`, `skills/`, `optional-skills/`

Skill/plugin ecosystem.

- `skills/` contains installed or bundled skills.
- `optional-skills/` contains optional skill packs.
- `plugins/` contains plugin implementations, especially memory providers.

### `website/`

Docusaurus documentation/marketing website. This is separate from the local
dashboard in `web/`.

### `packaging/`

Platform packaging scripts for Windows/macOS/Homebrew/default settings.

This is relevant if DAClaw becomes a desktop app: packaging should eventually
start from a stable backend/frontend runtime contract rather than directly from
the current dashboard command.

## Current DataAgent Layer

Current DataAgent-related pieces:

```text
agent/data_sources.py
tools/data_analysis_tool.py
agent/data_catalog.py
tools/data_catalog_tool.py
agent/analysis_audit.py
tools/analysis_audit_tool.py
run_agent.py analysis audit hook
toolsets.py data toolset
web/src/pages/WorkspacePage.tsx
```

This is best described as:

```text
general agent + data tools + Web workspace UI + final report audit
```

It is not yet:

```text
dedicated DataAgent runtime with analysis runs, artifacts, validation,
provenance, and reproducible workflow state
```

## Key Refactor Principle

Do not begin by moving many files. First extract stable concepts:

1. `Run`
2. `RunEvent`
3. `Artifact`
4. `Workspace`
5. `AgentMode`
6. `DataAnalysisWorkflow`

After these concepts exist, moving code becomes much safer because imports have
new stable targets.
