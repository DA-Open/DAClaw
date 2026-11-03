# DAClaw Docker image

A prebuilt image of DAClaw with the Data Agent, the web dashboard, Playwright's
headless Chromium and all Python/Node dependencies.

```
ghcr.io/da-open/daclaw:latest       # latest hermes_demo
ghcr.io/da-open/daclaw:v0.1.0       # a release tag
ghcr.io/da-open/daclaw:sha-<sha7>   # a specific commit
```

The image is `linux/amd64` only. It contains no configuration or credentials:
`config.yaml`, `.env`, API keys, sessions and uploads all live in a data
directory on the host that you mount at `/opt/data`.

## Quick start

```bash
docker pull ghcr.io/da-open/daclaw:latest
mkdir -p ~/daclaw-data
```

**1. First start** creates `config.yaml`, `.env` and `SOUL.md` in the data
directory from the bundled templates (`--help` just exits afterwards):

```bash
docker run --rm \
  -e DACLAW_UID=$(id -u) -e DACLAW_GID=$(id -g) \
  -v ~/daclaw-data:/opt/data \
  ghcr.io/da-open/daclaw:latest --help
```

**2. Point it at a model.** Edit the `model:` section at the top of
`~/daclaw-data/config.yaml` (see [Model configuration](#model-configuration)).

**3. Chat with the Data Agent:**

```bash
export OPENAI_API_KEY=sk-your-key-here
docker run -it --rm \
  -e DACLAW_UID=$(id -u) -e DACLAW_GID=$(id -g) \
  -e OPENAI_API_KEY \
  -v ~/daclaw-data:/opt/data \
  ghcr.io/da-open/daclaw:latest chat -t data-agent-chat
```

`-t data-agent-chat` enables the same `data_*` tools as the web UI; plain
`chat` starts without them. `-e OPENAI_API_KEY` without a value passes the
variable through from your shell, so the key does not land in your shell
history.

Everything after the image name is passed to the `daclaw` command, e.g.
`... ghcr.io/da-open/daclaw:latest chat -q "hello"`.

## Data directory

Mounted at `/opt/data` (`DACLAW_HOME`). On first start the entrypoint creates
whatever is missing and never overwrites existing files.

| Path | Contents |
|------|----------|
| `config.yaml` | Main configuration, including the model section. Created with mode 600. |
| `.env` | API keys and other secrets. Created with mode 600. |
| `SOUL.md` | Agent persona. |
| `skills/` | Bundled skills, synced on every start; your edits are kept. |
| `sessions/`, `logs/`, `memories/`, `workspace/` … | Runtime state. |
| `web_uploads/` | Files uploaded through the dashboard. |
| `demo_data/` | Data files for the Data Agent to read from the CLI. |

The Data Agent only reads data sources from `demo_data/`, `web_uploads/` and
the install directory, so for `chat` put your CSV/Excel/Parquet/DuckDB files
in `~/daclaw-data/demo_data/`, or mount a directory there:
`-v ~/datasets:/opt/data/demo_data:ro`. Refer to them by their path inside
the container, e.g. `/opt/data/demo_data/sales.csv`.

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `DACLAW_UID`, `DACLAW_GID` | Strongly recommended | Your host user's uid/gid (`id -u`, `id -g`). The entrypoint chowns the data directory to this user and runs DAClaw as it. Without them the directory is chowned to uid 10000 and you lose access to your own `config.yaml` and `.env`. |
| `OPENAI_API_KEY` | Yes, for `provider: custom` | Key for the OpenAI-compatible endpoint set as `model.base_url`. Other providers use their own variable (`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, …; see `.env.example`). |
| `DACLAW_LLM_CONFIG` | For `/etl`, NL2SQL and synthesis | Path *inside the container* to the Data Agent's JSON model config, e.g. `/opt/data/llm_config.json` (see below). |

You can also write the variables into `~/daclaw-data/.env` instead of passing
`-e`. Values in `.env` take precedence over `-e` for the same variable.

## Model configuration

Any OpenAI-compatible endpoint (OpenAI, a relay/proxy, vLLM, …) works with
`provider: custom`. Replace the defaults at the top of `config.yaml`:

```yaml
model:
  default: "gpt-4o-mini"                  # model name the endpoint serves
  provider: "custom"
  base_url: "https://api.example.com/v1"  # your endpoint, including /v1
  api_mode: "chat_completions"
```

- The key comes from `OPENAI_API_KEY`; you can instead set
  `model.api_key`, but then `config.yaml` holds a secret.
- `api_mode: chat_completions` uses `/v1/chat/completions`, which every
  OpenAI-compatible server implements. Many relays do not fully implement the
  Responses API, so set it explicitly rather than relying on auto-detection.
- For a local server on the host (vLLM, Ollama, LM Studio) use
  `http://host.docker.internal:<port>/v1` and add
  `--add-host=host.docker.internal:host-gateway` to `docker run` on Linux.

### Data Agent model (`DACLAW_LLM_CONFIG`)

Some Data Agent paths (`/etl` / auto-dbt, NL2SQL, result synthesis) do not
read `config.yaml` yet; they use a separate JSON file. Create
`~/daclaw-data/llm_config.json` with mode 600:

```json
{
  "client_type": "OpenAI",
  "client_args": {
    "base_url": "https://api.example.com/v1",
    "api_key": "sk-your-key-here"
  },
  "request_args": {
    "model": "gpt-4o-mini"
  }
}
```

and add `DACLAW_LLM_CONFIG=/opt/data/llm_config.json` to `.env` (or pass it
with `-e`). `api_key` may also be a list of keys, which are rotated on retry.

## Web dashboard

`docker-compose.yml` in the repository root runs the dashboard:

```bash
export DACLAW_UID=$(id -u) DACLAW_GID=$(id -g)
export DACLAW_IMAGE=ghcr.io/da-open/daclaw:latest
docker compose up -d --no-build
# open http://127.0.0.1:9119
```

It mounts `./data` as the data directory. Or with plain `docker run`:

```bash
docker run -d --name daclaw \
  -e DACLAW_UID=$(id -u) -e DACLAW_GID=$(id -g) \
  -v ~/daclaw-data:/opt/data \
  -p 127.0.0.1:9119:9119 \
  ghcr.io/da-open/daclaw:latest dashboard --host 0.0.0.0 --port 9119 --no-open --insecure
```

Keep the host side on `127.0.0.1`. `--insecure` is needed only because the
dashboard listens on `0.0.0.0` inside the container. On a shared multi-user
machine, anyone who can open the page gets a session token that can read the
keys in `.env`, so stop the container when you are done there.

## Building the image yourself

```bash
docker build -t daclaw:local --build-arg VCS_REF=$(git rev-parse HEAD) .
```

| Build arg | Default | Purpose |
|-----------|---------|---------|
| `NPM_REGISTRY` | `https://registry.npmjs.org` | npm registry, e.g. `https://registry.npmmirror.com` on slow networks. |
| `INSTALL_WHATSAPP_BRIDGE` | `true` | `false` skips the WhatsApp bridge, which fetches a dependency from GitHub. |
| `VCS_REF` | `unknown` | Source commit recorded in the `org.opencontainers.image.revision` label. |

Build args are visible in `docker history`: never pass keys or URLs containing
tokens. Behind a proxy, pass `--network host` plus the standard
`http_proxy`/`https_proxy`/`no_proxy` build args, which Docker does not record.

## Publishing (maintainers)

`.github/workflows/docker-publish.yml` builds and smoke-tests the image on
PRs that touch it, and pushes it to `ghcr.io/da-open/daclaw` on pushes to
`hermes_demo` and on `v*` tags, using the workflow's `GITHUB_TOKEN`. New GHCR
packages start out private: after the first publish, an organization owner
sets the package's visibility to **Public** under the organization's
*Packages → daclaw → Package settings*. The organization must allow public
packages (*Settings → Packages*).
