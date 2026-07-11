"""Render an animated, self-contained data story artifact."""

from __future__ import annotations

import html
import json
from typing import Any, Dict, List

from agent.data_agent import viz
from agent.data_agent.artifacts import ResultTable


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _narrative(text: str) -> str:
    paragraphs = [part.strip() for part in str(text or "").split("\n") if part.strip()]
    return "".join(f"<p>{_esc(part)}</p>" for part in paragraphs)


def render_story_html(
    title: str,
    slides: List[Dict[str, Any]],
    *,
    subtitle: str = "",
    inline_echarts: bool = True,
) -> str:
    """Build a cinematic HTML presentation with keyboard and autoplay controls."""
    rendered: List[str] = []
    chart_scripts: List[str] = []
    for index, slide in enumerate(slides):
        kind = str(slide.get("type") or "narrative")
        heading = _esc(slide.get("title") or f"Scene {index + 1}")
        kicker = _esc(slide.get("kicker") or kind.replace("_", " "))
        body = ""
        if kind == "chart" and isinstance(slide.get("result"), ResultTable):
            option = viz.build_chart_option(
                slide["result"],
                chart_type=slide.get("chart_type"),
                title="",
                subtitle="",
            )
            if option.get("_table"):
                body = viz._table_html(option["columns"], option["rows"], limit=40)
            else:
                chart_id = f"story_chart_{index}"
                body = f'<div id="{chart_id}" class="story-chart"></div>'
                chart_scripts.append(
                    f"const c{index}=echarts.init(document.getElementById('{chart_id}'));"
                    f"c{index}.setOption({json.dumps(option, ensure_ascii=False)});charts.push(c{index});"
                )
        elif kind == "table" and isinstance(slide.get("result"), ResultTable):
            body = viz._table_html(slide["result"].column_names, slide["result"].rows, limit=24)
        elif kind == "kpi":
            items = slide.get("items") or []
            body = '<div class="kpi-grid">' + "".join(
                f'<div class="kpi"><span>{_esc(item.get("label"))}</span><strong>{_esc(item.get("value"))}</strong><small>{_esc(item.get("detail"))}</small></div>'
                for item in items
            ) + "</div>"
        else:
            body = f'<div class="narrative">{_narrative(slide.get("text") or slide.get("body") or "")}</div>'
        rendered.append(
            f'<section class="scene" data-index="{index}"><div class="scene-inner">'
            f'<div class="scene-copy"><span class="kicker">{kicker}</span><h2>{heading}</h2>'
            f'{_narrative(slide.get("summary") or "")}</div><div class="scene-visual">{body}</div>'
            f'</div></section>'
        )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>{viz._echarts_script_tag(inline_echarts)}
<style>
:root{{--ink:#f8fbff;--muted:#8ea0ba;--line:#233451;--cyan:#54e6d1;--blue:#4f8cff;--violet:#9d7cff;}}
*{{box-sizing:border-box}} body{{margin:0;overflow:hidden;background:#07111f;color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,sans-serif}}
body:before{{content:"";position:fixed;inset:0;background:linear-gradient(rgba(71,101,148,.09) 1px,transparent 1px),linear-gradient(90deg,rgba(71,101,148,.09) 1px,transparent 1px);background-size:42px 42px;mask-image:linear-gradient(to bottom,black,transparent 92%);pointer-events:none}}
body:after{{content:"";position:fixed;inset:-30%;background:radial-gradient(circle at 30% 30%,rgba(79,140,255,.18),transparent 28%),radial-gradient(circle at 76% 66%,rgba(157,124,255,.16),transparent 30%);filter:blur(30px);pointer-events:none}}
.story{{height:100vh;position:relative;z-index:1}} .scene{{position:absolute;inset:0;padding:7vh 7vw 12vh;opacity:0;transform:translateX(5%) scale(.985);pointer-events:none;transition:opacity .55s ease,transform .7s cubic-bezier(.2,.8,.2,1)}}
.scene.active{{opacity:1;transform:none;pointer-events:auto}} .scene-inner{{height:100%;display:grid;grid-template-columns:minmax(280px,.85fr) minmax(0,1.6fr);gap:5vw;align-items:center}}
.scene-copy{{align-self:center}} .kicker{{display:inline-flex;padding:7px 11px;border:1px solid rgba(84,230,209,.35);border-radius:999px;color:var(--cyan);font-size:11px;font-weight:800;letter-spacing:.16em;text-transform:uppercase;background:rgba(84,230,209,.07)}}
h1,h2{{margin:18px 0 14px;letter-spacing:-.045em;line-height:.98}} h1{{font-size:clamp(48px,7vw,104px)}} h2{{font-size:clamp(38px,5vw,76px)}} .scene-copy p,.narrative p{{color:var(--muted);font-size:clamp(16px,1.5vw,22px);line-height:1.65;max-width:720px}}
.scene-visual{{min-height:54vh;display:flex;align-items:center;justify-content:center;padding:28px;border:1px solid rgba(108,139,190,.24);border-radius:28px;background:linear-gradient(145deg,rgba(13,28,50,.9),rgba(9,19,35,.68));box-shadow:0 30px 90px rgba(0,0,0,.35),inset 0 1px rgba(255,255,255,.04);backdrop-filter:blur(18px)}}
.story-chart{{width:100%;height:58vh}} .kpi-grid{{width:100%;display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:18px}} .kpi{{padding:24px;border:1px solid var(--line);border-radius:20px;background:rgba(5,14,27,.55)}} .kpi span{{display:block;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.12em}} .kpi strong{{display:block;margin:14px 0 6px;font-size:clamp(30px,4vw,58px);color:var(--cyan)}} .kpi small{{color:#a8b8ce}}
.data-table{{width:100%;border-collapse:collapse;font-size:13px}} .data-table th,.data-table td{{padding:10px;border-bottom:1px solid var(--line);text-align:left}} .data-table th{{color:var(--cyan);font-size:11px;text-transform:uppercase;letter-spacing:.08em}} .data-table td{{color:#dbe7f7}}
.topbar{{position:fixed;z-index:5;left:7vw;right:7vw;top:26px;display:flex;align-items:center;gap:16px}} .brand{{font-weight:900;letter-spacing:-.03em}} .brand i{{color:var(--cyan);font-style:normal}} .topbar .sub{{color:var(--muted);font-size:12px}} .count{{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums}}
.controls{{position:fixed;z-index:5;left:7vw;right:7vw;bottom:28px;display:flex;align-items:center;gap:12px}} button{{border:1px solid var(--line);background:rgba(8,21,39,.8);color:var(--ink);border-radius:12px;padding:10px 14px;cursor:pointer}} button:hover{{border-color:var(--cyan);color:var(--cyan)}} .progress{{height:3px;flex:1;background:var(--line);overflow:hidden;border-radius:9px}} .progress span{{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--blue),var(--cyan),var(--violet));transition:width .45s ease}}
@media(max-width:800px){{.scene{{padding:12vh 22px 14vh;overflow:auto}}.scene-inner{{grid-template-columns:1fr;gap:20px}}.scene-visual{{min-height:42vh}}.story-chart{{height:42vh}}}}
</style></head><body>
<div class="topbar"><div class="brand">DAClaw <i>Story</i></div><div class="sub">{_esc(subtitle)}</div><div class="count"><span id="current">01</span> / {len(rendered):02d}</div></div>
<main class="story"><section class="scene active" data-index="-1"><div class="scene-inner"><div class="scene-copy"><span class="kicker">Executive data story</span><h1>{_esc(title)}</h1><p>{_esc(subtitle or 'Grounded analysis, transformed into a cinematic narrative.')}</p></div><div class="scene-visual"><div class="narrative"><p>Use the controls below or the arrow keys to move through evidence, insights, and recommendations.</p></div></div></div></section>{''.join(rendered)}</main>
<div class="controls"><button id="prev">←</button><button id="play">Play</button><div class="progress"><span id="bar"></span></div><button id="next">→</button></div>
<script>
const scenes=[...document.querySelectorAll('.scene')],charts=[];let index=0,timer=null;{''.join(chart_scripts)}
function show(next){{index=(next+scenes.length)%scenes.length;scenes.forEach((s,i)=>s.classList.toggle('active',i===index));document.getElementById('current').textContent=String(index+1).padStart(2,'0');document.getElementById('bar').style.width=((index+1)/scenes.length*100)+'%';setTimeout(()=>charts.forEach(c=>c.resize()),180)}}
document.getElementById('prev').onclick=()=>show(index-1);document.getElementById('next').onclick=()=>show(index+1);document.getElementById('play').onclick=(e)=>{{if(timer){{clearInterval(timer);timer=null;e.target.textContent='Play'}}else{{timer=setInterval(()=>show(index+1),5000);e.target.textContent='Pause'}}}};document.addEventListener('keydown',e=>{{if(e.key==='ArrowRight'||e.key===' ')show(index+1);if(e.key==='ArrowLeft')show(index-1)}});window.addEventListener('resize',()=>charts.forEach(c=>c.resize()));show(0);
</script></body></html>"""

