#!/usr/bin/env bash
# Switch the DAClaw web demo's default model (the agentic chat at /data-agent).
#
# The web agent reads ~/.daclaw/config.yaml's `model:` key. Custom (Ark/doubao)
# providers ALSO need a matching entry in config.yaml `custom_providers:` and
# their API key in the current environment or ~/.daclaw/.env under the entry's
# `key_env`. This script validates credentials without writing them, updates the
# config for known-good presets, then reminds you to restart.
#
# Usage:
#   ./scripts/demo/set-model.sh gpt5        # gpt-5.5 via ModelHub/aidp (default)
#   ./scripts/demo/set-model.sh doubao21    # doubao-seed-2.1-pro via Ark
#   ./scripts/demo/set-model.sh doubao20    # doubao-seed-2.0-pro via Ark
#   ./scripts/demo/set-model.sh show        # print the current model
set -euo pipefail
CFG="$HOME/.daclaw/config.yaml"
ENVF="$HOME/.daclaw/.env"
PY="${DACLAW_PYTHON:-$HOME/miniconda3/envs/agent/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

choice="${1:-show}"

"$PY" - "$CFG" "$ENVF" "$choice" <<'PYEOF'
import sys, yaml, os, datetime
cfg_path, env_path, choice = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = yaml.safe_load(open(cfg_path)) or {}


def env_file_value(path, key):
    if not os.path.exists(path):
        return None
    for raw in open(path):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, sep, value = line.partition("=")
        if sep and name.strip() == key and value.strip():
            return value.strip().strip('"').strip("'")
    return None

# Presets may require an existing credential, but never contain one.
PRESETS = {
    "gpt5": {
        "model": {
            "provider": "aidp",
            "default": "gpt-5.5-2026-04-24",
            "base_url": "https://aidp.bytedance.net/api/modelhub/online/v2/crawl",
            "api_mode": "azure_chat_completions",
            "api_version": "2024-02-01",
        },
    },
    "doubao21": {
        "model": {"provider": "custom:volcengine-ark-21",
                  "default": "doubao-seed-2-1-pro-260628",
                  "base_url": "https://ark-cn-beijing.bytedance.net/api/v3"},
        "custom_provider": {"name": "volcengine-ark-21",
                            "base_url": "https://ark-cn-beijing.bytedance.net/api/v3",
                            "key_env": "ARK21_API_KEY", "api_mode": "chat_completions",
                            "model": "doubao-seed-2-1-pro-260628"},
        "required_env": "ARK21_API_KEY",
    },
    "doubao20": {
        "model": {"provider": "custom:volcengine-ark",
                  "default": "doubao-seed-2-0-pro-260215",
                  "base_url": "https://ark.cn-beijing.volces.com/api/v3"},
        "custom_provider": {"name": "volcengine-ark",
                            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                            "key_env": "ARK_API_KEY", "api_mode": "chat_completions",
                            "model": "doubao-seed-2-0-pro-260215"},
    },
}

if choice == "show":
    print("Current model:", cfg.get("model"))
    print("Presets:", ", ".join(k for k in PRESETS))
    sys.exit(0)

if choice not in PRESETS:
    print(f"Unknown preset '{choice}'. Options: {', '.join(PRESETS)}, show")
    sys.exit(1)

p = PRESETS[choice]
required_env = p.get("required_env")
if required_env and not (
    os.environ.get(required_env) or env_file_value(env_path, required_env)
):
    print(
        f"Missing {required_env}. Export it or add it to {env_path}; "
        "the script will not write credentials for you.",
        file=sys.stderr,
    )
    sys.exit(2)

ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
open(cfg_path + f".bak.{ts}", "w").write(open(cfg_path).read())

old = cfg.get("model")
cfg["model"] = p["model"]

# upsert custom_providers entry
if "custom_provider" in p:
    lst = cfg.get("custom_providers")
    if not isinstance(lst, list):
        lst = []
    cpe = p["custom_provider"]
    lst = [e for e in lst if not (isinstance(e, dict) and e.get("name") == cpe["name"])]
    lst.append(cpe)
    cfg["custom_providers"] = lst

yaml.safe_dump(cfg, open(cfg_path, "w"), allow_unicode=True, sort_keys=False)

print(f"model: {old}\n    -> {cfg['model']}")
PYEOF

if [ "$choice" != "show" ]; then
  echo ""
  echo "✓ Switched. Restart the backend to apply:  ./scripts/demo/start.sh"
fi
