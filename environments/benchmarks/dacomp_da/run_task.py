"""Run one DAComp-DA task with the DAClaw agent. Executed INSIDE the daclaw image.

The host orchestrator (bench.py) starts one fresh container per task attempt with:
  /work                         writable scratch (chart HTML, cwd, web workspace root)
  /data/<id>.sqlite             the task database (its cache dir, read-only bind mount)
  /opt/data                     fresh per-attempt DACLAW_HOME (config.yaml + filtered .env)
  /out                          submission dir: <id>.md, <id>-traj.txt, figure_NN.png
  /meta                         task.json (input), meta.json + messages.json (output)
  /bench                        this code, read-only
Gold rubrics / reference reports are never mounted.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, "/opt/daclaw")

PROMPT_VERSION = "v1"
TOOLSET = "data-agent-chat"
OBS_LIMIT = 3000
ARGS_LIMIT = 2000
IMAGE_REF = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")

PROMPT_TEMPLATE = """You are being evaluated on a data-analysis task. Its SQLite database is already connected (file: {db_path}; tables: {tables}). Use your data tools to investigate it.

Task ({instance_id}):
{instruction}

Deliverable rules:
- Your final reply IS the report. Write it directly in Markdown: no surrounding code fence, no HTML.
- Every number in the report must come from a query you actually ran.
- data_visualize and data_dashboard return a `png_file`. To include a chart, embed it exactly as given in `markdown_embed`, e.g. ![caption](figure_01.png). Do not reference HTML files or any image a tool did not return.
- No user is available to answer questions. State your assumptions and proceed."""

_render_lock = threading.Lock()
_figures: list[dict] = []

_WAIT_CHARTS_JS = """async () => {
  if (!window.echarts) return {instances: 0, timedOut: 0};
  const doms = [...document.querySelectorAll('[_echarts_instance_]')];
  const insts = doms.map(d => echarts.getInstanceByDom(d)).filter(Boolean);
  let timedOut = 0;
  await Promise.all(insts.map(i => new Promise(res => {
    const t = setTimeout(() => { timedOut += 1; res(); }, 5000);
    i.on('finished', () => { clearTimeout(t); res(); });
    i.setOption({animation: false});
    i.resize();
  })));
  return {instances: insts.length, timedOut};
}"""


def render_png(html_path: str, out_dir: Path, title: str) -> dict:
    """Render a chart/dashboard HTML to the next figure_NN.png in out_dir."""
    from playwright.sync_api import sync_playwright

    with _render_lock:
        name = f"figure_{len(_figures) + 1:02d}.png"
        rec = {"file": name, "title": title, "html": Path(html_path).name}
        try:
            html_text = Path(html_path).read_text(encoding="utf-8", errors="replace")
            expects_chart = "echarts.init" in html_text
            with sync_playwright() as p:
                browser = p.chromium.launch()
                try:
                    page = browser.new_page(viewport={"width": 1100, "height": 700}, device_scale_factor=2)
                    errors: list[str] = []
                    page.on("pageerror", lambda e: errors.append(str(e)[:300]))
                    page.goto(Path(html_path).resolve().as_uri(), wait_until="load", timeout=30000)
                    # The provenance SQL block is useful in the UI but is noise in a report figure.
                    page.add_style_tag(content=".sql-block{display:none !important}")
                    state = page.evaluate(_WAIT_CHARTS_JS)
                    target = page.query_selector(".wrap")
                    if target:
                        target.screenshot(path=str(out_dir / name))
                    else:
                        page.screenshot(path=str(out_dir / name), full_page=True)
                finally:
                    browser.close()
            size = (out_dir / name).stat().st_size
            rec.update(ok=True, bytes=size, echarts_instances=state["instances"],
                       wait_timeouts=state["timedOut"], page_errors=errors)
            problems = []
            if expects_chart and state["instances"] == 0:
                problems.append("no echarts instance rendered")
            if errors:
                problems.append("page errors")
            if size < 2000:
                problems.append("suspiciously small png")
            if problems:
                rec["warning"] = "; ".join(problems)
        except Exception as exc:  # export failure is recorded, never silently skipped
            rec.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:500])
        _figures.append(rec)
        return rec


def install_png_export(out_dir: Path) -> None:
    """Wrap chart tools so each chart is also exported as a PNG the report can embed."""
    import model_tools  # noqa: F401  # triggers tool discovery so the entries exist before we wrap them
    from tools.registry import registry

    for tool_name in ("data_visualize", "data_dashboard"):
        entry = registry.get_entry(tool_name)
        if entry is None:
            raise RuntimeError(f"tool {tool_name} not registered; cannot install PNG export")
        original = entry.handler

        def handler(args, _orig=original, _name=tool_name, **kwargs):
            raw = _orig(args, **kwargs)
            try:
                data = json.loads(raw)
            except Exception:
                return raw
            if not isinstance(data, dict) or not data.get("html_path"):
                return raw
            title = str(args.get("title") or "chart")
            rec = render_png(data["html_path"], out_dir, title)
            rec["tool"] = _name
            if rec.get("ok"):
                data["png_file"] = rec["file"]
                data["markdown_embed"] = f"![{title}]({rec['file']})"
            else:
                data["png_export_error"] = "PNG export failed; do not reference this chart as an image."
            return json.dumps(data, ensure_ascii=False)

        entry.handler = handler


def _text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or f"[{part.get('type', 'part')}]"))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return str(content)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"... [truncated {len(text) - limit} chars]"


def format_trajectory(messages: list, final: str) -> str:
    """Step-by-step text in the style of the official baselines' <id>-traj.txt."""
    lines: list[str] = []
    step = -1
    for msg in messages:
        role = msg.get("role")
        if role == "user":
            lines += ["--- Task ---", _text(msg.get("content")), ""]
        elif role == "assistant":
            step += 1
            lines.append(f"--- Step {step} ---")
            thought = _text(msg.get("content")).strip()
            if thought:
                lines.append(f"thought: {thought}")
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                lines.append(f"action: {fn.get('name')}({_clip(str(fn.get('arguments', '')), ARGS_LIMIT)})")
        elif role == "tool":
            lines.append(f"observation: {_clip(_text(msg.get('content')), OBS_LIMIT)}")
    lines += ["", "--- Final Answer ---", final or "(no final answer)"]
    return "\n".join(lines) + "\n"


def tool_errors(messages: list) -> list:
    """Tool results that came back as errors, with the tool name that produced them."""
    names = {}
    for msg in messages:
        for call in msg.get("tool_calls") or []:
            names[call.get("id")] = (call.get("function") or {}).get("name")
    errors = []
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        text = _text(msg.get("content"))
        try:
            data = json.loads(text)
        except Exception:
            continue
        if isinstance(data, dict) and (data.get("error") or data.get("success") is False):
            errors.append({"tool": names.get(msg.get("tool_call_id")) or msg.get("name"),
                           "error": str(data.get("error") or data)[:500]})
    return errors


def strip_outer_fence(text: str) -> tuple[str, bool]:
    m = re.fullmatch(r"\s*```(?:markdown|md)?\s*\n(.*?)\n```\s*", text, flags=re.S)
    return (m.group(1), True) if m else (text, False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="")
    ap.add_argument("--max-iterations", type=int, default=0, help="0 = product setting (agent.max_turns)")
    ap.add_argument("--temperature", type=float, default=None)
    args = ap.parse_args()

    task = json.loads(Path("/meta/task.json").read_text(encoding="utf-8"))
    iid = task["instance_id"]
    db_path = f"/data/{iid}.sqlite"
    out_dir = Path("/out")
    meta: dict = {"instance_id": iid, "prompt_version": PROMPT_VERSION, "toolset": TOOLSET,
                  "max_iterations": args.max_iterations, "temperature": args.temperature}
    t0 = time.time()
    status = "error"
    final = ""
    messages: list = []
    try:
        from tools.skills_sync import sync_skills
        sync_skills(quiet=True)  # fresh per-task copy of the bundled skills

        from agent.data_agent.engine import get_engine
        eng = get_engine()
        desc = eng.connect_source(db_path, persist=False)
        # Lock DuckDB file access: the agent can query attached tables but cannot
        # read other files in the container (e.g. the API key in /opt/data/.env).
        eng.con.execute("SET enable_external_access=false")
        eng.con.execute("SET lock_configuration=true")
        meta["tables"] = desc["tables"]

        install_png_export(out_dir)

        # Drive the agent exactly the way the web Data Agent (/api/da/agent) does, so the
        # score reflects the real product harness: same AIAgent factory (SOUL, tool
        # guidance, skills index, platform="web", audit setting, fallback model) and the
        # same Data Agent system message with the connected schema. Context files are
        # read from TERMINAL_CWD (= /work), so the repo's AGENTS.md is not injected.
        from daclaw_cli import web_server

        agent = web_server._create_web_chat_agent(
            session_id=None,
            clarify_callback=lambda question, choices: (
                "(no response — proceed with your best assumption)"
            ),
            enabled_toolsets_override=[TOOLSET],
        )
        if args.model:
            agent.model = args.model
        if args.max_iterations:
            agent.max_iterations = args.max_iterations  # run_conversation rebuilds the budget from it
        if args.temperature is not None:
            agent.request_overrides["temperature"] = args.temperature
        system_message = web_server._da_agent_system_prompt()
        model = agent.model
        meta["tools"] = sorted(agent.valid_tool_names)
        meta["model"] = model
        meta["harness"] = "web_server._create_web_chat_agent + _da_agent_system_prompt"
        meta["max_iterations"] = agent.max_iterations
        # Written before the loop so a timed-out attempt still records its config;
        # DAClaw itself saves the transcript to $DACLAW_HOME/sessions/ after every turn.
        Path("/meta/meta.json").write_text(json.dumps(dict(meta, status="running"), ensure_ascii=False,
                                                      indent=2, default=str), encoding="utf-8")
        prompt = PROMPT_TEMPLATE.format(db_path=db_path, tables=", ".join(desc["tables"]),
                                        instance_id=iid, instruction=task["instruction"])
        result = agent.run_conversation(prompt, system_message=system_message, task_id=iid)
        messages = result.get("messages") or []
        final = (result.get("final_response") or "").strip()
        for key in ("model", "provider", "base_url", "api_calls", "completed", "interrupted",
                    "input_tokens", "output_tokens", "reasoning_tokens", "prompt_tokens",
                    "completion_tokens", "total_tokens", "estimated_cost_usd", "cost_status"):
            meta[key] = result.get(key)
        if result.get("error"):
            meta["agent_error"] = str(result["error"])[:1000]
        if not final:
            # No answer: an API-level failure is retryable infra, otherwise it is the agent's result.
            status = "api_error" if result.get("error") or result.get("failed") else "no_answer"
        elif not result.get("completed"):
            status = "incomplete"
        else:
            status = "ok"
    except Exception as exc:
        meta["exception"] = traceback.format_exc()[-4000:]
        meta["agent_error"] = f"{type(exc).__name__}: {exc}"[:1000]

    final, fence_stripped = strip_outer_fence(final)
    meta["fence_stripped"] = fence_stripped
    answer = final if final else f"(No answer produced. Run status: {status}.)"
    (out_dir / f"{iid}.md").write_text(answer + "\n", encoding="utf-8")
    (out_dir / f"{iid}-traj.txt").write_text(format_trajectory(messages, final), encoding="utf-8")

    refs = IMAGE_REF.findall(answer)
    meta["image_refs"] = refs
    meta["missing_image_refs"] = [r for r in refs if not (out_dir / r).is_file()]
    meta["figures"] = _figures
    meta["tool_errors"] = tool_errors(messages)
    meta["status"] = status
    meta["duration_s"] = round(time.time() - t0, 1)
    Path("/meta/messages.json").write_text(json.dumps(messages, ensure_ascii=False, default=str), encoding="utf-8")
    Path("/meta/meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"instance_id": iid, "status": status, "duration_s": meta["duration_s"]}))
    return 0 if status in ("ok", "incomplete", "no_answer") else 3


if __name__ == "__main__":
    sys.exit(main())
