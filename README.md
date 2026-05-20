<p align="center">
  <img src="assets/banner.png" alt="DAClaw Agent" width="100%">
</p>

# DAClaw Agent ☤

<p align="center">
  <a href="https://da-open.github.io/docs/"><img src="https://img.shields.io/badge/Docs-da--open.github.io-2DD4BF?style=for-the-badge" alt="Documentation"></a>
  <a href="https://github.com/DA-Open/DAClaw/issues"><img src="https://img.shields.io/badge/Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/DA-Open/DAClaw/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License: MIT"></a>
  <a href="https://github.com/DA-Open"><img src="https://img.shields.io/badge/Built%20by-DA--Open-blueviolet?style=for-the-badge" alt="Built by DA-Open"></a>
</p>

**The self-improving AI agent built by [DA-Open](https://github.com/DA-Open).** It's the only agent with a built-in learning loop — it creates skills from experience, improves them during use, nudges itself to persist knowledge, searches its own past conversations, and builds a deepening model of who you are across sessions. Run it on a $5 VPS, a GPU cluster, or serverless infrastructure that costs nearly nothing when idle. It's not tied to your laptop — talk to it from Telegram while it works on a cloud VM.

Use any model you want — DA-Open ModelHub, [OpenRouter](https://openrouter.ai) (200+ models), [Xiaomi MiMo](https://platform.xiaomimimo.com), [z.ai/GLM](https://z.ai), [Kimi/Moonshot](https://platform.moonshot.ai), [MiniMax](https://www.minimax.io), [Hugging Face](https://huggingface.co), OpenAI, or your own endpoint. Switch with `daclaw model` — no code changes, no lock-in.

<table>
<tr><td><b>A real terminal interface</b></td><td>Full TUI with multiline editing, slash-command autocomplete, conversation history, interrupt-and-redirect, and streaming tool output.</td></tr>
<tr><td><b>Lives where you do</b></td><td>Telegram, Discord, Slack, WhatsApp, Signal, and CLI — all from a single gateway process. Voice memo transcription, cross-platform conversation continuity.</td></tr>
<tr><td><b>A closed learning loop</b></td><td>Agent-curated memory with periodic nudges. Autonomous skill creation after complex tasks. Skills self-improve during use. FTS5 session search with LLM summarization for cross-session recall. <a href="https://github.com/plastic-labs/honcho">Honcho</a> dialectic user modeling. Compatible with the <a href="https://agentskills.io">agentskills.io</a> open standard.</td></tr>
<tr><td><b>Scheduled automations</b></td><td>Built-in cron scheduler with delivery to any platform. Daily reports, nightly backups, weekly audits — all in natural language, running unattended.</td></tr>
<tr><td><b>Delegates and parallelizes</b></td><td>Spawn isolated subagents for parallel workstreams. Write Python scripts that call tools via RPC, collapsing multi-step pipelines into zero-context-cost turns.</td></tr>
<tr><td><b>Runs anywhere, not just your laptop</b></td><td>Six terminal backends — local, Docker, SSH, Daytona, Singularity, and Modal. Daytona and Modal offer serverless persistence — your agent's environment hibernates when idle and wakes on demand, costing nearly nothing between sessions. Run it on a $5 VPS or a GPU cluster.</td></tr>
<tr><td><b>Research-ready</b></td><td>Batch trajectory generation, Atropos RL environments, trajectory compression for training the next generation of tool-calling models.</td></tr>
</table>

---

## Quick Install

```bash
curl -fsSL https://raw.githubusercontent.com/DA-Open/DAClaw/main/scripts/install.sh | bash
```

Works on Linux, macOS, WSL2, and Android via Termux. The installer handles the platform-specific setup for you.

> **Android / Termux:** The tested manual path is documented in the [Termux guide](https://da-open.github.io/docs/getting-started/termux). On Termux, DAClaw installs a curated `.[termux]` extra because the full `.[all]` extra currently pulls Android-incompatible voice dependencies.
>
> **Windows:** Native Windows is not supported. Please install [WSL2](https://learn.microsoft.com/en-us/windows/wsl/install) and run the command above.

After installation:

```bash
source ~/.bashrc    # reload shell (or: source ~/.zshrc)
daclaw              # start chatting!
```

---

## Getting Started

```bash
daclaw              # Interactive CLI — start a conversation
daclaw model        # Choose your LLM provider and model
daclaw tools        # Configure which tools are enabled
daclaw config set   # Set individual config values
daclaw gateway      # Start the messaging gateway (Telegram, Discord, etc.)
daclaw setup        # Run the full setup wizard (configures everything at once)
daclaw claw migrate # Migrate from OpenClaw (if coming from OpenClaw)
daclaw update       # Update to the latest version
daclaw doctor       # Diagnose any issues
```

📖 **[Full documentation →](https://da-open.github.io/docs/)**

## Web UI

DAClaw includes a local web dashboard with a chat page, file upload, visible tool-call progress, configuration, API key management, sessions, logs, and analytics.

Install the web dependencies if they are not already installed:

```bash
pip install -e ".[web]"
# or, from an installed package:
pip install "daclaw-agent[web]"
```

Configure a model before starting the UI:

```bash
daclaw setup model
# or:
daclaw model
```

Start the local dashboard:

```bash
daclaw dashboard
```

By default this opens:

```text
http://127.0.0.1:9119
```

The chat interface is available at:

```text
http://127.0.0.1:9119/chat
```

Useful options:

```bash
daclaw dashboard --no-open          # Start server without opening a browser
daclaw dashboard --port 9120        # Use another port
daclaw dashboard --host 127.0.0.1   # Bind to localhost explicitly
```

Uploaded files are stored under `~/.daclaw/web_uploads/`. SQLite datasets such as `.sqlite`, `.sqlite3`, `.db`, and `.db3` can be uploaded for analysis. For large local datasets, the default upload limit is 1024 MB; override it with `DACLAW_WEB_UPLOAD_MAX_MB` if needed.

For a from-scratch Ubuntu ECS smoke test that exposes the dashboard over a temporary public IP, see [docs/ecs-web-dashboard-smoke-test.md](docs/ecs-web-dashboard-smoke-test.md).

## CLI vs Messaging Quick Reference

DAClaw has two entry points: start the terminal UI with `daclaw`, or run the gateway and talk to it from Telegram, Discord, Slack, WhatsApp, Signal, or Email. Once you're in a conversation, many slash commands are shared across both interfaces.

| Action | CLI | Messaging platforms |
|---------|-----|---------------------|
| Start chatting | `daclaw` | Run `daclaw gateway setup` + `daclaw gateway start`, then send the bot a message |
| Start fresh conversation | `/new` or `/reset` | `/new` or `/reset` |
| Change model | `/model [provider:model]` | `/model [provider:model]` |
| Set a personality | `/personality [name]` | `/personality [name]` |
| Retry or undo the last turn | `/retry`, `/undo` | `/retry`, `/undo` |
| Compress context / check usage | `/compress`, `/usage`, `/insights [--days N]` | `/compress`, `/usage`, `/insights [days]` |
| Browse skills | `/skills` or `/<skill-name>` | `/skills` or `/<skill-name>` |
| Interrupt current work | `Ctrl+C` or send a new message | `/stop` or send a new message |
| Platform-specific status | `/platforms` | `/status`, `/sethome` |

For the full command lists, see the [CLI guide](https://da-open.github.io/docs/user-guide/cli) and the [Messaging Gateway guide](https://da-open.github.io/docs/user-guide/messaging).

---

## Documentation

All documentation lives at **[da-open.github.io/docs](https://da-open.github.io/docs/)**:

| Section | What's Covered |
|---------|---------------|
| [Quickstart](https://da-open.github.io/docs/getting-started/quickstart) | Install → setup → first conversation in 2 minutes |
| [CLI Usage](https://da-open.github.io/docs/user-guide/cli) | Commands, keybindings, personalities, sessions |
| [Configuration](https://da-open.github.io/docs/user-guide/configuration) | Config file, providers, models, all options |
| [Messaging Gateway](https://da-open.github.io/docs/user-guide/messaging) | Telegram, Discord, Slack, WhatsApp, Signal, Home Assistant |
| [Security](https://da-open.github.io/docs/user-guide/security) | Command approval, DM pairing, container isolation |
| [Tools & Toolsets](https://da-open.github.io/docs/user-guide/features/tools) | 40+ tools, toolset system, terminal backends |
| [Skills System](https://da-open.github.io/docs/user-guide/features/skills) | Procedural memory, Skills Hub, creating skills |
| [Memory](https://da-open.github.io/docs/user-guide/features/memory) | Persistent memory, user profiles, best practices |
| [MCP Integration](https://da-open.github.io/docs/user-guide/features/mcp) | Connect any MCP server for extended capabilities |
| [Cron Scheduling](https://da-open.github.io/docs/user-guide/features/cron) | Scheduled tasks with platform delivery |
| [Context Files](https://da-open.github.io/docs/user-guide/features/context-files) | Project context that shapes every conversation |
| [Architecture](https://da-open.github.io/docs/developer-guide/architecture) | Project structure, agent loop, key classes |
| [Contributing](https://da-open.github.io/docs/developer-guide/contributing) | Development setup, PR process, code style |
| [CLI Reference](https://da-open.github.io/docs/reference/cli-commands) | All commands and flags |
| [Environment Variables](https://da-open.github.io/docs/reference/environment-variables) | Complete env var reference |

---

## Migrating from OpenClaw

If you're coming from OpenClaw, DAClaw can automatically import your settings, memories, skills, and API keys.

**During first-time setup:** The setup wizard (`daclaw setup`) automatically detects `~/.openclaw` and offers to migrate before configuration begins.

**Anytime after install:**

```bash
daclaw claw migrate              # Interactive migration (full preset)
daclaw claw migrate --dry-run    # Preview what would be migrated
daclaw claw migrate --preset user-data   # Migrate without secrets
daclaw claw migrate --overwrite  # Overwrite existing conflicts
```

What gets imported:
- **SOUL.md** — persona file
- **Memories** — MEMORY.md and USER.md entries
- **Skills** — user-created skills → `~/.daclaw/skills/openclaw-imports/`
- **Command allowlist** — approval patterns
- **Messaging settings** — platform configs, allowed users, working directory
- **API keys** — allowlisted secrets (Telegram, OpenRouter, OpenAI, Anthropic, ElevenLabs)
- **TTS assets** — workspace audio files
- **Workspace instructions** — AGENTS.md (with `--workspace-target`)

See `daclaw claw migrate --help` for all options, or use the `openclaw-migration` skill for an interactive agent-guided migration with dry-run previews.

---

## Contributing

We welcome contributions! See the [Contributing Guide](https://da-open.github.io/docs/developer-guide/contributing) for development setup, code style, and PR process.

Quick start for contributors:

```bash
git clone https://github.com/DA-Open/DAClaw.git
cd daclaw-agent
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv venv --python 3.11
source venv/bin/activate
uv pip install -e ".[all,dev]"
python -m pytest tests/ -q
```

> **RL Training (optional):** To work on the RL/Tinker-Atropos integration:
> ```bash
> git submodule update --init tinker-atropos
> uv pip install -e "./tinker-atropos"
> ```

---

## Community

- 💬 [Discord](https://github.com/DA-Open/DAClaw/issues)
- 📚 [Skills Hub](https://agentskills.io)
- 🐛 [Issues](https://github.com/DA-Open/DAClaw/issues)
- 💡 [Discussions](https://github.com/DA-Open/DAClaw/discussions)
- 🔌 [DAClaw](https://github.com/AaronWong1999/daclawclaw) — Community WeChat bridge: Run DAClaw Agent and OpenClaw on the same WeChat account.

---

## License

MIT — see [LICENSE](LICENSE).

Built by [DA-Open](https://github.com/DA-Open).
