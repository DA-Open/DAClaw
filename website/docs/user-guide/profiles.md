---
sidebar_position: 2
---

# Profiles: Running Multiple Agents

Run multiple independent DAClaw agents on the same machine — each with its own config, API keys, memory, sessions, skills, and gateway.

## What are profiles?

A profile is a fully isolated DAClaw environment. Each profile gets its own directory containing its own `config.yaml`, `.env`, `SOUL.md`, memories, sessions, skills, cron jobs, and state database. Profiles let you run separate agents for different purposes — a coding assistant, a personal bot, a research agent — without any cross-contamination.

When you create a profile, it automatically becomes its own command. Create a profile called `coder` and you immediately have `coder chat`, `coder setup`, `coder gateway start`, etc.

## Quick start

```bash
daclaw profile create coder       # creates profile + "coder" command alias
coder setup                       # configure API keys and model
coder chat                        # start chatting
```

That's it. `coder` is now a fully independent agent. It has its own config, its own memory, its own everything.

## Creating a profile

### Blank profile

```bash
daclaw profile create mybot
```

Creates a fresh profile with bundled skills seeded. Run `mybot setup` to configure API keys, model, and gateway tokens.

### Clone config only (`--clone`)

```bash
daclaw profile create work --clone
```

Copies your current profile's `config.yaml`, `.env`, and `SOUL.md` into the new profile. Same API keys and model, but fresh sessions and memory. Edit `~/.hermes/profiles/work/.env` for different API keys, or `~/.hermes/profiles/work/SOUL.md` for a different personality.

### Clone everything (`--clone-all`)

```bash
daclaw profile create backup --clone-all
```

Copies **everything** — config, API keys, personality, all memories, full session history, skills, cron jobs, plugins. A complete snapshot. Useful for backups or forking an agent that already has context.

### Clone from a specific profile

```bash
daclaw profile create work --clone --clone-from coder
```

:::tip Honcho memory + profiles
When Honcho is enabled, `--clone` automatically creates a dedicated AI peer for the new profile while sharing the same user workspace. Each profile builds its own observations and identity. See [Honcho -- Multi-agent / Profiles](./features/memory-providers.md#honcho) for details.
:::

## Using profiles

### Command aliases

Every profile automatically gets a command alias at `~/.local/bin/<name>`:

```bash
coder chat                    # chat with the coder agent
coder setup                   # configure coder's settings
coder gateway start           # start coder's gateway
coder doctor                  # check coder's health
coder skills list             # list coder's skills
coder config set model.model anthropic/claude-sonnet-4
```

The alias works with every daclaw subcommand — it's just `daclaw -p <name>` under the hood.

### The `-p` flag

You can also target a profile explicitly with any command:

```bash
daclaw -p coder chat
daclaw --profile=coder doctor
daclaw chat -p coder -q "hello"    # works in any position
```

### Sticky default (`daclaw profile use`)

```bash
daclaw profile use coder
daclaw chat                   # now targets coder
daclaw tools                  # configures coder's tools
daclaw profile use default    # switch back
```

Sets a default so plain `daclaw` commands target that profile. Like `kubectl config use-context`.

### Knowing where you are

The CLI always shows which profile is active:

- **Prompt**: `coder ❯` instead of `❯`
- **Banner**: Shows `Profile: coder` on startup
- **`daclaw profile`**: Shows current profile name, path, model, gateway status

## Running gateways

Each profile runs its own gateway as a separate process with its own bot token:

```bash
coder gateway start           # starts coder's gateway
assistant gateway start       # starts assistant's gateway (separate process)
```

### Different bot tokens

Each profile has its own `.env` file. Configure a different Telegram/Discord/Slack bot token in each:

```bash
# Edit coder's tokens
nano ~/.hermes/profiles/coder/.env

# Edit assistant's tokens
nano ~/.hermes/profiles/assistant/.env
```

### Safety: token locks

If two profiles accidentally use the same bot token, the second gateway will be blocked with a clear error naming the conflicting profile. Supported for Telegram, Discord, Slack, WhatsApp, and Signal.

### Persistent services

```bash
coder gateway install         # creates hermes-gateway-coder systemd/launchd service
assistant gateway install     # creates hermes-gateway-assistant service
```

Each profile gets its own service name. They run independently.

## Configuring profiles

Each profile has its own:

- **`config.yaml`** — model, provider, toolsets, all settings
- **`.env`** — API keys, bot tokens
- **`SOUL.md`** — personality and instructions

```bash
coder config set model.model anthropic/claude-sonnet-4
echo "You are a focused coding assistant." > ~/.hermes/profiles/coder/SOUL.md
```

## Updating

`daclaw update` pulls code once (shared) and syncs new bundled skills to **all** profiles automatically:

```bash
daclaw update
# → Code updated (12 commits)
# → Skills synced: default (up to date), coder (+2 new), assistant (+2 new)
```

User-modified skills are never overwritten.

## Managing profiles

```bash
daclaw profile list           # show all profiles with status
daclaw profile show coder     # detailed info for one profile
daclaw profile rename coder dev-bot   # rename (updates alias + service)
daclaw profile export coder   # export to coder.tar.gz
daclaw profile import coder.tar.gz   # import from archive
```

## Deleting a profile

```bash
daclaw profile delete coder
```

This stops the gateway, removes the systemd/launchd service, removes the command alias, and deletes all profile data. You'll be asked to type the profile name to confirm.

Use `--yes` to skip confirmation: `daclaw profile delete coder --yes`

:::note
You cannot delete the default profile (`~/.hermes`). To remove everything, use `daclaw uninstall`.
:::

## Tab completion

```bash
# Bash
eval "$(daclaw completion bash)"

# Zsh
eval "$(daclaw completion zsh)"
```

Add the line to your `~/.bashrc` or `~/.zshrc` for persistent completion. Completes profile names after `-p`, profile subcommands, and top-level commands.

## How it works

Profiles use the `HERMES_HOME` environment variable. When you run `coder chat`, the wrapper script sets `HERMES_HOME=~/.hermes/profiles/coder` before launching hermes. Since 119+ files in the codebase resolve paths via `get_hermes_home()`, everything automatically scopes to the profile's directory — config, sessions, memory, skills, state database, gateway PID, logs, and cron jobs.

The default profile is simply `~/.hermes` itself. No migration needed — existing installs work identically.
