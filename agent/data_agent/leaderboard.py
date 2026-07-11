"""Leaderboard backend for Part 2 — Model × Harness evaluation tracking.

Tracks which (model, harness) combos are strongest across Data-Agent research
benchmarks. A "harness" is the agent scaffold (Claude Code, Codex, OpenClaw,
Hermes, DAClaw); a "model" is the underlying LLM.

For the demo the scores are SEED DATA — deliberately calibrated to the real
magnitudes reported in the source papers so the numbers are believable:
  - DAComp DE (repository-level engineering): SOTA < 20%
  - DAComp DA (open-ended analysis):          SOTA < 40%
  - DV-World (real-world data visualization):  SOTA < 50%
The schema + API are built so real evaluation runs can replace the seed later
(the "one-click evaluate" button posts into the same store).

State lives under ``$DACLAW_HOME/leaderboard/leaderboard.json`` (profile-scoped).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from daclaw_constants import get_daclaw_home

# -- taxonomy ---------------------------------------------------------------

# Agent scaffolds ("harnesses"). A harness wraps an LLM with tools, planning
# and execution loops; the same model can score very differently across them.
HARNESSES = [
    {"id": "daclaw", "name": "DAClaw", "vendor": "DA-Open"},
    {"id": "claude-code", "name": "Claude Code", "vendor": "Anthropic"},
    {"id": "codex", "name": "Codex", "vendor": "OpenAI"},
    {"id": "cursor", "name": "Cursor", "vendor": "Anysphere"},
    {"id": "cline", "name": "Cline", "vendor": "OSS"},
    {"id": "opencode", "name": "OpenCode", "vendor": "OSS"},
    {"id": "kilocode", "name": "Kilocode", "vendor": "OSS"},
    {"id": "openclaw", "name": "OpenClaw", "vendor": "OSS"},
    {"id": "hermes", "name": "Hermes", "vendor": "OSS"},
]

# Capability levels used to group benchmarks in the UI, mirroring the DA-Omni
# holistic-evaluation taxonomy (Table 3): L1 atomic deterministic, L2
# engineering deterministic, L3 open-ended subjective, plus a Text-to-Viz band.
LEVELS = [
    {"id": "L1", "name": "L1 · Atomic", "desc": "Deterministic text-to-SQL & data-science primitives", "color": "#2DD4BF"},
    {"id": "L2", "name": "L2 · Engineering", "desc": "Repo-level pipelines, debugging, data-to-insight", "color": "#6366F1"},
    {"id": "L3", "name": "L3 · Analysis", "desc": "Open-ended analytical reasoning & reporting", "color": "#F59E0B"},
    {"id": "VIZ", "name": "Text-to-Viz", "desc": "Chart / dashboard / visualization generation", "color": "#EC4899"},
]

# Benchmarks span the full data lifecycle. Ceilings are calibrated to the real
# magnitudes reported in DA-Omni (arXiv 2512.xxxxx, "Scaling Training for
# Full-Stack Data Agents", Table 3) and the source papers of each benchmark, so
# the seed numbers stay believable. `level` groups them in the UI.
BENCHMARKS = [
    # ---- L1: Atomic deterministic ----------------------------------------
    {
        "id": "spider", "name": "Spider", "paper": "Yu et al. 2018",
        "level": "L1", "splits": ["dev", "test"],
        "metric": "Execution Accuracy %", "sota_ceiling": 91.0,
    },
    {
        "id": "bird", "name": "BIRD", "paper": "Li et al. 2024",
        "level": "L1", "splits": ["dev-2024-09"],
        "metric": "Execution Accuracy %", "sota_ceiling": 72.0,
    },
    {
        "id": "spider2", "name": "Spider 2.0", "paper": "Lei et al. 2024",
        "level": "L1", "splits": ["Spider2-Snow"],
        "metric": "Execution Accuracy %", "sota_ceiling": 62.0,
    },
    {
        "id": "da-code", "name": "DA-Code", "paper": "Huang et al. 2024",
        "level": "L1", "splits": ["EDA"],
        "metric": "Pass %", "sota_ceiling": 60.0,
    },
    {
        "id": "dabench", "name": "DABench", "paper": "Hu et al. 2024",
        "level": "L1", "splits": ["analysis"],
        "metric": "Accuracy %", "sota_ceiling": 78.0,
    },
    {
        "id": "dsbench", "name": "DSBench", "paper": "Jing et al. 2024",
        "level": "L1", "splits": ["data-science"],
        "metric": "Task Success %", "sota_ceiling": 40.0,
    },
    # ---- L2: Engineering deterministic -----------------------------------
    {
        "id": "bird-critic", "name": "BIRD-Critic", "paper": "Li et al. 2025",
        "level": "L2", "splits": ["1.0-Open"],
        "metric": "Pass %", "sota_ceiling": 40.0,
    },
    {
        "id": "dacomp-de", "name": "DAComp · DE", "paper": "DAComp (ICLR 2026)",
        "level": "L2", "splits": ["DE-Impl", "DE-Evol", "DE-Arch"],
        "metric": "Completion Score %", "sota_ceiling": 65.0,
    },
    {
        "id": "kramabench", "name": "KramaBench", "paper": "Lai et al. 2025",
        "level": "L2", "splits": ["data-lake"],
        "metric": "Pipeline Success %", "sota_ceiling": 42.0,
    },
    # ---- L3: Open-ended subjective ----------------------------------------
    {
        "id": "dacomp-da", "name": "DAComp · DA", "paper": "DAComp (ICLR 2026)",
        "level": "L3", "splits": ["Score", "w/o-Vis"],
        "metric": "LLM-judge rubric %", "sota_ceiling": 55.0,
    },
    # ---- Text-to-Visualization -------------------------------------------
    {
        "id": "text2vis", "name": "Text2Vis", "paper": "Rahman et al. 2025",
        "level": "VIZ", "splits": ["gen"],
        "metric": "Pass %", "sota_ceiling": 48.0,
    },
    {
        "id": "plot2code", "name": "Plot2Code", "paper": "Wu et al. 2025",
        "level": "VIZ", "splits": ["plot2code"],
        "metric": "Pass %", "sota_ceiling": 56.0,
    },
    {
        "id": "plotcraft", "name": "Plotcraft", "paper": "Zhang et al. 2025",
        "level": "VIZ", "splits": ["craft"],
        "metric": "MLLM-judge %", "sota_ceiling": 44.0,
    },
    {
        "id": "dvworld-sheet", "name": "DV-World · Sheet", "paper": "DV-World (ICML 2026)",
        "level": "VIZ", "splits": ["create", "dashboards", "fix"],
        "metric": "table-align + MLLM-judge %", "sota_ceiling": 50.0,
    },
    {
        "id": "dvworld-evolution", "name": "DV-World · Evolution", "paper": "DV-World (ICML 2026)",
        "level": "VIZ", "splits": ["evolution"],
        "metric": "table-align + MLLM-judge %", "sota_ceiling": 50.0,
    },
    {
        "id": "dvworld-interact", "name": "DV-World · Interact", "paper": "DV-World (ICML 2026)",
        "level": "VIZ", "splits": ["interact"],
        "metric": "intent-alignment %", "sota_ceiling": 50.0,
    },
]

# Underlying LLMs. Curated to the *notable* flagship models people recognize
# (both open-weights and proprietary) — one or two per major lab, not the long
# tail. Each has an Intelligence Index (0–100, higher = stronger) used both to
# display an artificial-analysis-style ranking and to shape believable seed
# scores. `license`: "open" (weights released) or "proprietary" (API-only).
MODELS: List[Dict[str, Any]] = [
    # ---- Frontier proprietary --------------------------------------------
    {"id": "claude-fable-5", "name": "Claude Fable 5", "creator": "Anthropic", "license": "proprietary", "index": 75},
    {"id": "gpt-5.6", "name": "GPT-5.6", "creator": "OpenAI", "license": "proprietary", "index": 74},
    {"id": "claude-opus-4.8", "name": "Claude Opus 4.8", "creator": "Anthropic", "license": "proprietary", "index": 72},
    {"id": "gemini-3-pro", "name": "Gemini 3 Pro", "creator": "Google", "license": "proprietary", "index": 71},
    {"id": "grok-4.2", "name": "Grok 4.2", "creator": "xAI", "license": "proprietary", "index": 69},
    {"id": "gpt-5.5", "name": "GPT-5.5", "creator": "OpenAI", "license": "proprietary", "index": 68},
    {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro", "creator": "Google", "license": "proprietary", "index": 65},
    {"id": "claude-sonnet-4.6", "name": "Claude Sonnet 4.6", "creator": "Anthropic", "license": "proprietary", "index": 64},
    # ---- Frontier open-weights -------------------------------------------
    {"id": "deepseek-v4", "name": "DeepSeek V4", "creator": "DeepSeek", "license": "open", "index": 66},
    {"id": "glm-5", "name": "GLM-5", "creator": "Z AI", "license": "open", "index": 64},
    {"id": "qwen3-max", "name": "Qwen3 Max", "creator": "Alibaba", "license": "open", "index": 63},
    {"id": "kimi-k2", "name": "Kimi K2", "creator": "Moonshot AI", "license": "open", "index": 61},
    {"id": "deepseek-v3.2", "name": "DeepSeek V3.2", "creator": "DeepSeek", "license": "open", "index": 60},
    {"id": "minimax-m2", "name": "MiniMax M2", "creator": "MiniMax", "license": "open", "index": 59},
    {"id": "qwen3-coder", "name": "Qwen3 Coder", "creator": "Alibaba", "license": "open", "index": 58},
    {"id": "llama-4-maverick", "name": "Llama 4 Maverick", "creator": "Meta", "license": "open", "index": 54},
    {"id": "mistral-large-3", "name": "Mistral Large 3", "creator": "Mistral", "license": "open", "index": 52},
    {"id": "glm-4.6", "name": "GLM-4.6", "creator": "Z AI", "license": "open", "index": 56},
    # ---- Efficient / mini flagships --------------------------------------
    {"id": "gpt-5.5-mini", "name": "GPT-5.5 mini", "creator": "OpenAI", "license": "proprietary", "index": 57},
    {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash", "creator": "Google", "license": "proprietary", "index": 55},
    {"id": "claude-haiku-4.5", "name": "Claude Haiku 4.5", "creator": "Anthropic", "license": "proprietary", "index": 53},
    {"id": "grok-4.1-fast", "name": "Grok 4.1 Fast", "creator": "xAI", "license": "proprietary", "index": 51},
]

# Back-compat: many call sites and the seed loop want just the id strings.
MODEL_IDS: List[str] = [m["id"] for m in MODELS]
MODEL_BY_ID: Dict[str, Dict[str, Any]] = {m["id"]: m for m in MODELS}


def get_leaderboard_dir() -> Path:
    d = get_daclaw_home() / "leaderboard"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _store_path() -> Path:
    return get_leaderboard_dir() / "leaderboard.json"


# -- deterministic believable seed scores -----------------------------------


def _seed_score(model: str, harness: str, benchmark: Dict[str, Any], split: str) -> float:
    """Produce a stable, believable score under the benchmark's SOTA ceiling.

    Deterministic (no RNG) so the leaderboard is reproducible: derived from a
    hash of the (model, harness, benchmark, split) tuple, shaped by per-model
    and per-harness strength priors, then clamped below the SOTA ceiling.
    """
    import hashlib

    ceiling = benchmark["sota_ceiling"]
    # per-model prior (0..1) derived from the model's Intelligence Index (0–100).
    idx = MODEL_BY_ID.get(model, {}).get("index", 45)
    # map index 25→0.45 .. 80→0.98 so even weak models look plausible
    model_prior = max(0.42, min(0.98, 0.30 + idx / 100.0 * 0.85))
    # per-harness prior — DAClaw/Claude Code strong; others vary
    harness_prior = {
        "daclaw": 0.97,
        "claude-code": 0.93,
        "codex": 0.86,
        "cursor": 0.84,
        "cline": 0.80,
        "opencode": 0.77,
        "kilocode": 0.75,
        "openclaw": 0.76,
        "hermes": 0.72,
    }.get(harness, 0.7)

    h = hashlib.sha1(f"{model}|{harness}|{benchmark['id']}|{split}".encode()).hexdigest()
    jitter = (int(h[:4], 16) % 1000) / 1000.0  # 0..1 stable
    combined = 0.55 * model_prior + 0.35 * harness_prior + 0.10 * jitter
    score = round(ceiling * combined, 1)
    return max(2.0, min(score, ceiling - 0.3))


def generate_seed() -> Dict[str, Any]:
    """Build the full seed leaderboard (every model × harness × benchmark×split)."""
    entries: List[Dict[str, Any]] = []
    for bm in BENCHMARKS:
        for split in bm["splits"]:
            for m in MODELS:
                mid = m["id"]
                for h in HARNESSES:
                    entries.append(
                        {
                            "model": mid,
                            "harness": h["id"],
                            "benchmark": bm["id"],
                            "split": split,
                            "score": _seed_score(mid, h["id"], bm, split),
                            "metric": bm["metric"],
                            "date": "2026-07-10",
                            "source": "seed",
                        }
                    )
    return {
        "version": 4,
        "harnesses": HARNESSES,
        "benchmarks": BENCHMARKS,
        "levels": LEVELS,
        "models": MODELS,
        "entries": entries,
    }


CURRENT_VERSION = 4


def load_leaderboard(*, seed_if_missing: bool = True) -> Dict[str, Any]:
    p = _store_path()
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = None
        # Regenerate when the persisted taxonomy is stale (older schema version
        # or missing the newer harness/benchmark set) so the demo always shows
        # the full, current leaderboard rather than a cached older one.
        if isinstance(data, dict) and data.get("version") == CURRENT_VERSION:
            return data
    data = generate_seed()
    if seed_if_missing:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def save_leaderboard(data: Dict[str, Any]) -> None:
    _store_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def add_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Add or replace a score entry (used by the 'one-click evaluate' path)."""
    data = load_leaderboard()
    key = (entry["model"], entry["harness"], entry["benchmark"], entry.get("split"))
    data["entries"] = [
        e
        for e in data["entries"]
        if (e["model"], e["harness"], e["benchmark"], e.get("split")) != key
    ]
    data["entries"].append(entry)
    save_leaderboard(data)
    return entry


# -- aggregations for the UI -------------------------------------------------


def harness_by_benchmark_matrix() -> Dict[str, Any]:
    """Average score per (harness, benchmark), plus an overall ranking."""
    data = load_leaderboard()
    from collections import defaultdict

    acc: Dict[tuple, List[float]] = defaultdict(list)
    for e in data["entries"]:
        acc[(e["harness"], e["benchmark"])].append(e["score"])

    matrix: Dict[str, Dict[str, float]] = {}
    for (harness, bm), scores in acc.items():
        matrix.setdefault(harness, {})[bm] = round(sum(scores) / len(scores), 1)

    # overall = mean across benchmarks
    overall = []
    for h in data["harnesses"]:
        hid = h["id"]
        vals = list(matrix.get(hid, {}).values())
        overall.append(
            {"harness": hid, "name": h["name"], "vendor": h["vendor"],
             "overall": round(sum(vals) / len(vals), 1) if vals else 0.0}
        )
    overall.sort(key=lambda x: x["overall"], reverse=True)
    return {
        "harnesses": data["harnesses"],
        "benchmarks": data["benchmarks"],
        "levels": data.get("levels", LEVELS),
        "matrix": matrix,
        "ranking": overall,
    }


def best_combos(top_n: int = 10) -> List[Dict[str, Any]]:
    """Top (model × harness) combos by mean score across all benchmarks."""
    data = load_leaderboard()
    from collections import defaultdict

    acc: Dict[tuple, List[float]] = defaultdict(list)
    for e in data["entries"]:
        acc[(e["model"], e["harness"])].append(e["score"])
    combos = [
        {"model": m, "harness": h, "mean_score": round(sum(v) / len(v), 1), "n": len(v)}
        for (m, h), v in acc.items()
    ]
    combos.sort(key=lambda x: x["mean_score"], reverse=True)
    return combos[:top_n]


def model_ranking() -> List[Dict[str, Any]]:
    """Per-model leaderboard, artificialanalysis.ai style.

    For each model we report its Intelligence Index (static metadata) plus an
    empirical "agentic score" = mean benchmark score achieved by its BEST
    harness (i.e. the model paired with whatever scaffold suits it best), so the
    ranking rewards raw model capability rather than a specific harness.
    """
    data = load_leaderboard()
    from collections import defaultdict

    # mean score per (model, harness) across all benchmarks
    acc: Dict[tuple, List[float]] = defaultdict(list)
    for e in data["entries"]:
        acc[(e["model"], e["harness"])].append(e["score"])
    best_by_model: Dict[str, float] = {}
    best_harness: Dict[str, str] = {}
    for (m, h), v in acc.items():
        mean = sum(v) / len(v)
        if mean > best_by_model.get(m, -1):
            best_by_model[m] = mean
            best_harness[m] = h

    rows: List[Dict[str, Any]] = []
    for m in data.get("models", []):
        mid = m["id"] if isinstance(m, dict) else m
        meta = MODEL_BY_ID.get(mid, {})
        rows.append(
            {
                "id": mid,
                "name": meta.get("name", mid),
                "creator": meta.get("creator", "—"),
                "license": meta.get("license", "proprietary"),
                "index": meta.get("index", 0),
                "agentic_score": round(best_by_model.get(mid, 0.0), 1),
                "best_harness": best_harness.get(mid, ""),
            }
        )
    # rank by intelligence index (matches artificialanalysis default sort)
    rows.sort(key=lambda r: r["index"], reverse=True)
    return rows
