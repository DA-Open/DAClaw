import { useEffect, useMemo, useRef, useState } from "react";
import {
  Wand2, Database, Loader2, Download, FileJson, FileSpreadsheet, FileText,
  Table2, Sparkles, History, X, ChevronRight,
} from "lucide-react";
import { api } from "@/lib/api";
import type { DATableInfo, DASynthesizeResponse } from "@/lib/api";
import { useI18n } from "@/i18n";

// Data Synthesis workbench (Research → Data Synthesis).
// Two pipelines:
//   • training  — turn a connected DB / SQL into LLM training data (text2sql / qa / instruction)
//   • table     — fabricate an arbitrary research-style table from a prompt
// Pick params (count, formats), run, preview + download, keep a local history.

type Mode = "training" | "table";
type Style = "text2sql" | "qa" | "instruction";
type Fmt = "jsonl" | "csv" | "xlsx" | "parquet";

const HISTORY_KEY = "daclaw.synth.history.v1";
const MAX_HISTORY = 15;

type SynthRun = { id: string; at: number; label: string; mode: Mode; result: DASynthesizeResponse };

function loadHistory(): SynthRun[] {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]") as SynthRun[]; } catch { return []; }
}
function persistHistory(h: SynthRun[]) {
  try { localStorage.setItem(HISTORY_KEY, JSON.stringify(h.slice(0, MAX_HISTORY))); } catch { /* quota */ }
}

const STYLE_LABEL: Record<Style, { en: string; zh: string; desc_en: string; desc_zh: string }> = {
  text2sql: { en: "Text-to-SQL", zh: "Text-to-SQL", desc_en: "NL question → SQL pairs", desc_zh: "自然语言问题 → SQL 配对" },
  qa: { en: "Q&A", zh: "问答", desc_en: "grounded question/answer", desc_zh: "基于数据的问答" },
  instruction: { en: "Instruction", zh: "指令微调", desc_en: "instruction/input/output triples", desc_zh: "指令/输入/输出三元组" },
};

function fmtIcon(f: string) {
  if (f === "jsonl") return <FileJson className="h-3.5 w-3.5 text-amber-300" />;
  if (f === "xlsx") return <FileSpreadsheet className="h-3.5 w-3.5 text-emerald-400" />;
  if (f === "csv") return <FileText className="h-3.5 w-3.5 text-cyan-300" />;
  return <Database className="h-3.5 w-3.5 text-violet-300" />;
}

const TABLE_EXAMPLES = [
  "A clinical trial patient cohort: demographics, treatment arm, baseline vitals, outcomes.",
  "E-commerce transactions with customer, product, price, discount, channel, and timestamp.",
  "IoT sensor readings: device id, location, temperature, humidity, battery, reading time.",
  "Academic papers: title, authors, venue, year, citations, field, abstract snippet.",
];

export default function SynthesisPage() {
  const { locale } = useI18n();
  const zh = locale === "zh";
  const [mode, setMode] = useState<Mode>("training");
  const [style, setStyle] = useState<Style>("text2sql");
  const [tables, setTables] = useState<DATableInfo[]>([]);
  const [selTable, setSelTable] = useState<string>("");
  const [sql, setSql] = useState("");
  const [spec, setSpec] = useState("");
  const [extra, setExtra] = useState("");
  const [count, setCount] = useState(20);
  const [formats, setFormats] = useState<Fmt[]>(["jsonl"]);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<DASynthesizeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<SynthRun[]>(loadHistory);
  const [historyOpen, setHistoryOpen] = useState(false);
  const seq = useRef(0);

  useEffect(() => { api.daTables().then((r) => setTables(r.tables)).catch(() => {}); }, []);

  const toggleFmt = (f: Fmt) =>
    setFormats((s) => (s.includes(f) ? s.filter((x) => x !== f) : [...s, f]));

  const canRun = useMemo(() => {
    if (busy) return false;
    if (mode === "table") return spec.trim().length > 0;
    return tables.length > 0 || sql.trim().length > 0;
  }, [busy, mode, spec, tables.length, sql]);

  const run = async () => {
    if (!canRun) return;
    const mine = ++seq.current;
    setBusy(true); setError(null);
    const payload: Record<string, unknown> = {
      mode, count, formats,
      extra: extra.trim() || undefined,
    };
    if (mode === "training") {
      payload.style = style;
      if (selTable) payload.tables = [selTable];
      if (sql.trim()) payload.sql = sql.trim();
    } else {
      payload.spec = spec.trim();
    }
    try {
      const r = await api.daSynthesize(payload);
      if (mine !== seq.current) return;
      if (r.ok) {
        setResult(r);
        const label = mode === "table" ? spec.trim().slice(0, 48) : `${style} · ${selTable || "schema"}`;
        const run: SynthRun = { id: `s_${Date.now().toString(36)}`, at: Date.now(), label, mode, result: r };
        setHistory((h) => { const n = [run, ...h].slice(0, MAX_HISTORY); persistHistory(n); return n; });
      } else {
        setError(r.error || "synthesis failed");
      }
    } catch (e) {
      if (mine === seq.current) setError(String(e));
    } finally {
      if (mine === seq.current) setBusy(false);
    }
  };

  const deleteRun = (id: string) => setHistory((h) => { const n = h.filter((r) => r.id !== id); persistHistory(n); return n; });

  return (
    <div className="mission-shell relative flex h-full min-h-0 bg-[#1a2536] text-slate-100">
      {/* config column */}
      <div className="flex w-[400px] shrink-0 flex-col border-r border-white/5 bg-[#1e2a3d]/60">
        <div className="flex items-center gap-2 border-b border-white/5 px-4 py-3">
          <Wand2 className="h-4 w-4 text-[#2DD4BF]" />
          <span className="text-sm font-semibold">{zh ? "数据合成" : "Data Synthesis"}</span>
          <button
            onClick={() => setHistoryOpen((v) => !v)}
            className={`ml-auto inline-flex items-center gap-1 rounded-lg border px-2 py-1 text-[11px] ${historyOpen ? "border-[#2DD4BF] bg-[#2DD4BF]/10 text-[#2DD4BF]" : "border-white/10 text-slate-400 hover:bg-white/5"}`}
          >
            <History className="h-3 w-3" /> {zh ? "历史" : "History"} {history.length > 0 && `· ${history.length}`}
          </button>
        </div>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
          {/* pipeline picker */}
          <div>
            <div className="mb-1.5 text-[10px] font-bold uppercase tracking-wide text-slate-500">{zh ? "合成管线" : "Pipeline"}</div>
            <div className="grid grid-cols-2 gap-2">
              <button onClick={() => setMode("training")} className={`rounded-xl border p-3 text-left ${mode === "training" ? "border-[#2DD4BF] bg-[#2DD4BF]/10" : "border-white/10 hover:bg-white/5"}`}>
                <Sparkles className="h-4 w-4 text-[#2DD4BF]" />
                <div className="mt-1 text-xs font-bold">{zh ? "LLM 训练数据" : "LLM training data"}</div>
                <div className="text-[10px] text-slate-500">{zh ? "从库/SQL 生成" : "from a DB / SQL"}</div>
              </button>
              <button onClick={() => setMode("table")} className={`rounded-xl border p-3 text-left ${mode === "table" ? "border-[#2DD4BF] bg-[#2DD4BF]/10" : "border-white/10 hover:bg-white/5"}`}>
                <Table2 className="h-4 w-4 text-violet-300" />
                <div className="mt-1 text-xs font-bold">{zh ? "科研表格" : "Research table"}</div>
                <div className="text-[10px] text-slate-500">{zh ? "按描述生成" : "from a prompt"}</div>
              </button>
            </div>
          </div>

          {mode === "training" ? (
            <>
              {/* style */}
              <div>
                <div className="mb-1.5 text-[10px] font-bold uppercase tracking-wide text-slate-500">{zh ? "训练数据风格" : "Training style"}</div>
                <div className="space-y-1.5">
                  {(Object.keys(STYLE_LABEL) as Style[]).map((s) => (
                    <button key={s} onClick={() => setStyle(s)} className={`flex w-full items-center gap-2 rounded-lg border px-2.5 py-2 text-left ${style === s ? "border-[#2DD4BF] bg-[#2DD4BF]/10" : "border-white/10 hover:bg-white/5"}`}>
                      <div className="min-w-0 flex-1">
                        <div className="text-xs font-semibold">{zh ? STYLE_LABEL[s].zh : STYLE_LABEL[s].en}</div>
                        <div className="text-[10px] text-slate-500">{zh ? STYLE_LABEL[s].desc_zh : STYLE_LABEL[s].desc_en}</div>
                      </div>
                      {style === s && <ChevronRight className="h-3.5 w-3.5 text-[#2DD4BF]" />}
                    </button>
                  ))}
                </div>
              </div>
              {/* source */}
              <div>
                <div className="mb-1.5 text-[10px] font-bold uppercase tracking-wide text-slate-500">{zh ? "数据来源" : "Source"}</div>
                {tables.length > 0 ? (
                  <select value={selTable} onChange={(e) => setSelTable(e.target.value)} className="w-full rounded-lg border border-white/10 bg-[#141d2b] px-2.5 py-2 text-sm">
                    <option value="">{zh ? "全部已连接的表" : "All connected tables"}</option>
                    {tables.map((t) => <option key={t.table} value={t.table}>{t.table}{t.row_count != null ? ` (${t.row_count})` : ""}</option>)}
                  </select>
                ) : (
                  <div className="rounded-lg border border-dashed border-white/10 px-3 py-2 text-[11px] text-slate-500">{zh ? "未连接数据库 — 可在下方直接粘贴 SQL" : "No DB connected — paste SQL below instead"}</div>
                )}
                <textarea value={sql} onChange={(e) => setSql(e.target.value)} rows={2} placeholder={zh ? "可选:参考 SQL(围绕它生成变体)" : "Optional: a reference SQL to build variations around"} className="mt-2 w-full resize-none rounded-lg border border-white/10 bg-[#141d2b] px-2.5 py-2 font-mono text-[11px]" />
              </div>
            </>
          ) : (
            <div>
              <div className="mb-1.5 text-[10px] font-bold uppercase tracking-wide text-slate-500">{zh ? "表格描述" : "Table spec"}</div>
              <textarea value={spec} onChange={(e) => setSpec(e.target.value)} rows={4} placeholder={zh ? "描述你想要的表:领域、列、风格…" : "Describe the table: domain, columns, style…"} className="w-full resize-none rounded-lg border border-white/10 bg-[#141d2b] px-2.5 py-2 text-sm" />
              <div className="mt-2 flex flex-wrap gap-1.5">
                {TABLE_EXAMPLES.map((ex) => (
                  <button key={ex} onClick={() => setSpec(ex)} className="rounded-full border border-white/10 px-2 py-0.5 text-[10px] text-slate-400 hover:bg-white/5 hover:text-slate-200">
                    {ex.length > 34 ? ex.slice(0, 34) + "…" : ex}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* count + formats */}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <div className="mb-1.5 text-[10px] font-bold uppercase tracking-wide text-slate-500">{zh ? "数量" : "Count"}</div>
              <input type="number" min={1} max={2000} value={count} onChange={(e) => setCount(Math.max(1, Math.min(2000, Number(e.target.value) || 1)))} className="w-full rounded-lg border border-white/10 bg-[#141d2b] px-2.5 py-2 text-sm tabular-nums" />
            </div>
            <div>
              <div className="mb-1.5 text-[10px] font-bold uppercase tracking-wide text-slate-500">{zh ? "格式" : "Formats"}</div>
              <div className="flex flex-wrap gap-1">
                {(["jsonl", "csv", "xlsx", "parquet"] as Fmt[]).map((f) => (
                  <button key={f} onClick={() => toggleFmt(f)} className={`rounded-md border px-1.5 py-1 text-[10px] font-medium ${formats.includes(f) ? "border-[#2DD4BF] bg-[#2DD4BF]/10 text-[#2DD4BF]" : "border-white/10 text-slate-500 hover:bg-white/5"}`}>{f}</button>
                ))}
              </div>
            </div>
          </div>

          <div>
            <div className="mb-1.5 text-[10px] font-bold uppercase tracking-wide text-slate-500">{zh ? "额外要求(可选)" : "Extra guidance (optional)"}</div>
            <input value={extra} onChange={(e) => setExtra(e.target.value)} placeholder={zh ? "例如:只要英文、加入噪声、覆盖边界情况…" : "e.g. English only, include noise, cover edge cases…"} className="w-full rounded-lg border border-white/10 bg-[#141d2b] px-2.5 py-2 text-sm" />
          </div>
        </div>

        <div className="border-t border-white/5 p-3">
          <button onClick={run} disabled={!canRun} className="flex w-full items-center justify-center gap-2 rounded-lg bg-gradient-to-r from-blue-500 to-cyan-400 px-4 py-2.5 text-sm font-black text-[#041817] hover:opacity-90 disabled:opacity-40">
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Wand2 className="h-4 w-4" />}
            {busy ? (zh ? "合成中…" : "Synthesizing…") : (zh ? "开始合成" : "Synthesize")}
          </button>
        </div>
      </div>

      {/* result column */}
      <div className="min-w-0 flex-1 overflow-auto p-6">
        {busy && (
          <div className="flex h-full flex-col items-center justify-center gap-3 text-slate-500">
            <Loader2 className="h-8 w-8 animate-spin text-[#2DD4BF]" />
            <div className="text-sm">{zh ? "正在合成数据…" : "Synthesizing data…"}</div>
          </div>
        )}
        {!busy && error && <div className="m-2 rounded-lg border border-red-400/30 bg-red-400/10 p-4 text-sm text-red-300">{error}</div>}
        {!busy && !result && !error && (
          <div className="flex h-full flex-col items-center justify-center gap-3 text-center text-slate-500">
            <Wand2 className="h-10 w-10 text-[#2DD4BF]" />
            <div className="font-semibold text-slate-200">{zh ? "合成 LLM 训练数据 / 科研表格" : "Synthesize LLM training data / research tables"}</div>
            <p className="max-w-md text-sm">{zh ? "从已连接的数据库或一句描述,生成任意数量、任意格式的合成数据产物,供科研使用。" : "From a connected database or a one-line spec, generate any quantity of synthetic data in any format for research."}</p>
          </div>
        )}
        {!busy && result && (
          <div className="space-y-4">
            {/* summary + downloads */}
            <div className="rounded-2xl border border-white/10 bg-[#212e42]/70 p-4">
              <div className="flex items-center gap-2">
                <Sparkles className="h-4 w-4 text-[#2DD4BF]" />
                <span className="text-sm font-bold">{result.mode === "table" ? (zh ? "科研表格" : "Research table") : `${result.style}`}</span>
                <span className="rounded-full bg-white/5 px-2 py-0.5 text-[10px] text-slate-400">{result.n_rows} {zh ? "行" : "rows"}</span>
                {result.source && <span className="rounded-full bg-white/5 px-2 py-0.5 text-[10px] text-slate-500">{result.source === "llm" ? "LLM" : (zh ? "启发式" : "heuristic")}</span>}
              </div>
              {(result.files?.length ?? 0) > 0 && (
                <div className="mt-3 flex flex-wrap gap-2">
                  {result.files!.map((f) => (
                    <a key={f.file} href={f.download_url} download className="inline-flex items-center gap-1.5 rounded-lg border border-[#2DD4BF]/30 bg-[#2DD4BF]/5 px-3 py-1.5 text-xs font-medium hover:bg-[#2DD4BF]/10">
                      {fmtIcon(f.format)} {f.format.toUpperCase()} <Download className="h-3 w-3 text-slate-400" />
                    </a>
                  ))}
                </div>
              )}
            </div>
            {/* preview table */}
            {result.preview && result.preview.length > 0 && (
              <div className="overflow-hidden rounded-2xl border border-white/10">
                <div className="border-b border-white/5 bg-[#1e2a3d]/60 px-4 py-2 text-[11px] font-bold uppercase tracking-wide text-slate-400">{zh ? "预览" : "Preview"} · {zh ? "前" : "first"} {result.preview.length}</div>
                <div className="overflow-x-auto">
                  <table className="w-full text-[11px]">
                    <thead>
                      <tr className="border-b border-white/5 bg-white/[0.02]">
                        {(result.columns || Object.keys(result.preview[0])).map((c) => <th key={c} className="px-3 py-1.5 text-left font-semibold text-slate-300">{c}</th>)}
                      </tr>
                    </thead>
                    <tbody>
                      {result.preview.map((row, ri) => (
                        <tr key={ri} className="border-b border-white/5 last:border-0">
                          {(result.columns || Object.keys(result.preview![0])).map((c) => {
                            const v = (row as Record<string, unknown>)[c];
                            const s = v == null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v);
                            return <td key={c} className="max-w-[280px] truncate px-3 py-1.5 text-slate-400" title={s}>{s}</td>;
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* history drawer */}
      {historyOpen && (
        <div className="absolute right-0 top-12 z-[60] max-h-[70vh] w-80 overflow-y-auto rounded-bl-2xl border border-white/10 bg-[#1e2a3d] p-2 shadow-2xl">
          {history.length === 0 ? (
            <div className="px-2 py-4 text-center text-[11px] text-slate-500">{zh ? "暂无历史" : "No history yet"}</div>
          ) : history.map((r) => (
            <div key={r.id} className="group mb-1 flex items-center gap-2 rounded-lg border border-transparent px-2 py-1.5 hover:border-white/10 hover:bg-white/5">
              <button onClick={() => { setResult(r.result); setHistoryOpen(false); }} className="min-w-0 flex-1 text-left">
                <div className="truncate text-[11px] font-semibold text-slate-200">{r.label}</div>
                <div className="text-[9px] text-slate-500">{r.mode} · {r.result.n_rows} rows</div>
              </button>
              <button onClick={() => deleteRun(r.id)} className="shrink-0 text-slate-600 opacity-0 hover:text-red-400 group-hover:opacity-100"><X className="h-3.5 w-3.5" /></button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
