# DAClaw Repository Inventory

## 中文速读

这份文档回答一个最实际的问题：这个仓库里这么多文件，哪些是核心，哪些只是辅助，哪些可以先不用管，哪些可能是本地生成物。

先记住 5 个最重要的代码入口：

```text
run_agent.py              核心 Agent 主循环
model_tools.py            工具发现和工具调用分发
tools/                    具体工具实现
daclaw_cli/main.py        daclaw 命令入口
daclaw_cli/web_server.py  当前 Web 后端入口
web/                      当前 Web 前端
```

如果你只是想理解项目，第一轮只需要读这些：

```text
docs/current-architecture-map.md
run_agent.py 的 AIAgent 类大致结构
model_tools.py
tools/registry.py
toolsets.py
daclaw_cli/main.py
daclaw_cli/web_server.py
web/src/App.tsx
web/src/pages/WorkspacePage.tsx
web/src/pages/ChatPage.tsx
```

不要第一天就读完所有文件。这个仓库有很多平台适配、测试、技能、文档和打包脚本，不是每个都和你当前改造目标有关。

## Category A: Core Runtime

这些是 DAClaw 的核心运行逻辑。改它们会影响所有入口。

| Path | Useful? | What It Does |
| --- | --- | --- |
| `run_agent.py` | Must keep | Core `AIAgent` class and conversation loop. |
| `model_tools.py` | Must keep | Tool discovery, schema collection, and function-call dispatch. |
| `toolsets.py` | Must keep | Toolset definitions and shared core tool list. |
| `toolset_distributions.py` | Keep | Preset toolset distributions. |
| `daclaw_state.py` | Must keep | SQLite session persistence and search. |
| `daclaw_constants.py` | Must keep | Profile-safe path helpers. |
| `daclaw_logging.py` | Keep | Logging setup. |
| `daclaw_time.py` | Keep | Timezone-aware time helpers. |
| `trajectory_compressor.py` | Keep | Trajectory compression support. |
| `utils.py` | Keep | Shared helpers. |

## Category B: Agent Internals

| Path | Useful? | What It Does |
| --- | --- | --- |
| `agent/` | Must keep | Prompt building, compression, model metadata, auxiliary LLM calls, memory, DataAgent early pieces. |
| `agent/data_sources.py` | Keep | Dataset profiling abstraction. |
| `agent/data_catalog.py` | Keep | Structured data catalog. |
| `agent/analysis_audit.py` | Keep | Data report audit guardrail. |

## Category C: Tools

| Path | Useful? | What It Does |
| --- | --- | --- |
| `tools/` | Must keep | All model-callable tools live here. |
| `tools/registry.py` | Must keep | Central registry used by all tools. |
| `tools/environments/` | Keep | Terminal backends: local, Docker, SSH, Modal, Daytona, etc. |

For understanding, read `tools/registry.py` first, then one small tool like
`tools/data_analysis_tool.py`, then a larger tool like `tools/terminal_tool.py`.

## Category D: User Interfaces And Entrypoints

| Path | Useful? | What It Does |
| --- | --- | --- |
| `daclaw_cli/` | Must keep | `daclaw` command package, config commands, auth, dashboard backend. |
| `cli.py` | Keep for now | Large interactive terminal UI file. Legacy-heavy but still important. |
| `web/` | Must keep | React/Vite dashboard frontend. |
| `gateway/` | Keep | Telegram/Discord/Slack/WhatsApp/etc messaging gateway. |
| `acp_adapter/` | Keep | Editor integration via ACP. |
| `cron/` | Keep | Scheduled jobs. |
| `batch_runner.py` | Keep | Batch processing entry point. |
| `mcp_serve.py` | Keep | MCP server entry point. |
| `rl_cli.py` | Keep if RL matters | RL/training CLI. |
| `mini_swe_runner.py` | Optional/keep for now | Mini SWE-style runner. |

## Category E: Web And Website

These two are different:

| Path | Useful? | What It Does |
| --- | --- | --- |
| `web/` | Must keep | Local product/dashboard UI. This is what you want to turn into software. |
| `daclaw_cli/web_server.py` | Must keep, should split later | FastAPI backend for the local dashboard. |
| `website/` | Keep or archive depending on product plan | Docusaurus docs/marketing website, not the local app. |
| `daclaw_cli/web_dist/` | Generated, ignored | Built frontend output. Not tracked by git. |

## Category F: Skills And Plugins

| Path | Useful? | What It Does |
| --- | --- | --- |
| `skills/` | Keep | Bundled/installed skill library. Large, but important to agent behavior. |
| `optional-skills/` | Keep | Optional skill packs. |
| `plugins/` | Keep | Plugin implementations, especially memory plugins. |

These folders are large but not random. They are part of the inherited
agent ecosystem.

## Category G: Tests

| Path | Useful? | What It Does |
| --- | --- | --- |
| `tests/` | Must keep | Pytest suite. Big, but it protects refactors. |

When refactoring, prefer running targeted tests first, then full suite before
release.

## Category H: Packaging, Install, And Platform Support

| Path | Useful? | What It Does |
| --- | --- | --- |
| `pyproject.toml` | Must keep | Python packaging and entry points. |
| `uv.lock` | Keep | Locked Python dependency resolution. |
| `requirements.txt` | Keep | Compatibility install file. |
| `package.json`, `package-lock.json` | Keep for now | Root JS scripts/dependencies. |
| `packaging/` | Keep | Windows/macOS/Homebrew packaging work. |
| `scripts/` | Keep | Install/release/helper scripts. |
| `docker/` and `Dockerfile` | Keep | Container support. |
| `nix/`, `flake.nix`, `flake.lock` | Keep if Nix support matters | Nix packaging/dev support. |
| `.github/` | Keep | CI/release workflows and GitHub metadata. |

## Category I: Docs, Plans, Reports

| Path | Useful? | What It Does |
| --- | --- | --- |
| `README.md` | Keep | Public overview. |
| `AGENTS.md` | Must keep | Instructions for coding agents working in this repo. |
| `docs/` | Keep | Technical docs. |
| `plans/` | Keep or merge into `docs/plans/` | Planning docs. |
| `future work.md` | Keep but repair encoding | Existing architecture/future-work notes, currently encoding-damaged. |
| `reports/` | Optional | Example/generated report. One tracked report currently exists. |
| `multiagent_recent_papers.md`, `recent_multiagent_claw_papers.md` | Optional research notes | Useful if research direction matters; otherwise can move to `docs/research/`. |
| `daclaw-already-has-routines.md` | Optional notes | Product/architecture note. |

## Category J: Local Generated Or Ignored State

These are not tracked by git or are ignored. They are usually safe to ignore
while understanding the source code.

| Path | Status | What It Is |
| --- | --- | --- |
| `venv/` | Ignored | Local Python virtual environment. |
| `.pytest_cache/` | Ignored | Pytest cache. |
| `__pycache__/` | Ignored | Python bytecode cache. |
| `hermes_cli/` | Untracked cache only | Leftover legacy directory with only `__pycache__`. Candidate for local cleanup. |
| `reports1111/` | Untracked cache only | Local generated/cache directory. Candidate for local cleanup. |
| `daclaw_agent.egg-info/` | Ignored | Editable install/build metadata. |
| `data/` | Ignored | Local datasets. Currently contains a large SQLite dataset. |
| `.daclaw-dashboard-*.log` | Ignored | Local dashboard logs. |
| `daclaw_cli/web_dist/` | Ignored | Built Web frontend output. |

Do not commit these unless you intentionally change the repository policy.

## Category K: Tracked Generated Artifacts To Review

These are tracked in git but may not belong in long-term source control.

| Path | Current Status | Recommendation |
| --- | --- | --- |
| `dist/daclaw_agent-0.9.0.tar.gz` | Tracked release archive | Review. Usually release archives belong in GitHub Releases, not source. |
| `dist/daclaw_agent-0.9.0-py3-none-any.whl` | Tracked wheel | Review. Usually generated package artifacts should not be committed. |
| `reports/dacomp-zh-001-credit-risk-report.md` | Tracked generated/example report | Keep only if it is an intentional demo fixture. |

Do not delete these blindly because they are tracked. Decide policy first.

## Suggested Cleanup Without Breaking Code

Safe first cleanup policy:

1. Do not move core code yet.
2. Keep all tracked files.
3. Delete or ignore only local generated files after confirming they are not
   needed for your current experiment.
4. Move knowledge notes into docs before deleting old notes.
5. Split large files by adding new modules and compatibility shims, not by a
   single huge rename.

Candidate local-only cleanup commands, if you want them later:

```powershell
Remove-Item -Recurse -Force hermes_cli, reports1111, __pycache__, .pytest_cache
Remove-Item -Force .daclaw-dashboard-*.log
```

I have not run these commands. They are listed for future manual cleanup only.

## Recommended Reading Order

To understand the architecture, read in this order:

1. `docs/current-architecture-map.md`
2. `docs/repository-inventory.md`
3. `pyproject.toml`
4. `daclaw_cli/main.py`
5. `run_agent.py` around `class AIAgent`
6. `run_agent.py` around `run_conversation`
7. `model_tools.py`
8. `tools/registry.py`
9. `toolsets.py`
10. `daclaw_cli/web_server.py`
11. `web/src/App.tsx`
12. `web/src/pages/WorkspacePage.tsx`
13. `web/src/lib/api.ts`

## If You Want To Refactor Later

The clean target structure should eventually look more like this:

```text
agent/runtime/        shared run loop, events, artifacts
agent/data_agent/     DataAgent workflow and validation
daclaw_cli/web/       split FastAPI app and routes
web/                  frontend only
tools/                model-callable tools only
gateway/              messaging adapters only
acp_adapter/          editor adapter only
```

But the first goal is understanding and classification, not moving everything.

