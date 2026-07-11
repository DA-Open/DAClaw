import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { useNavigate } from "react-router-dom";
import {
  Boxes,
  Database,
  Play,
  Plug,
  Loader2,
  Download,
  X,
  Layers,
  History,
  Sparkles,
  CheckCircle2,
  XCircle,
  MinusCircle,
  ArrowUpRight,
  Trash2,
} from "lucide-react";
import { api } from "@/lib/api";
import type { DbtBuildResponse, DbtModel, DbtDagNode, DATableInfo } from "@/lib/api";
import { SqlCode } from "@/components/SqlCode";
import { ConnectDbDialog } from "@/components/ConnectDbDialog";
import { EtlChatPanel } from "@/components/EtlChatPanel";
import { useI18n } from "@/i18n";
import type { Translations } from "@/i18n";

type DaT = Translations["da"];

const LAYER_ORDER = ["raw", "staging", "intermediate", "marts"] as const;
const LAYER_LABEL: Record<string, string> = {
  raw: "Raw Sources",
  staging: "Staging",
  intermediate: "Intermediate",
  marts: "Marts",
};
function layerLabel(layer: string, T: DaT): string {
  if (layer === "raw") return T.layerRaw;
  if (layer === "staging") return T.layerStaging;
  if (layer === "intermediate") return T.layerIntermediate;
  if (layer === "marts") return T.layerMarts;
  return LAYER_LABEL[layer] ?? layer;
}
const LAYER_COLOR: Record<string, string> = {
  raw: "#94a3b8",
  staging: "#2DD4BF",
  intermediate: "#6366f1",
  marts: "#f59e0b",
};

function StatusIcon({ status }: { status?: string }) {
  if (status === "ok") return <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />;
  if (status === "error") return <XCircle className="h-3.5 w-3.5 text-red-500" />;
  if (status === "skipped") return <MinusCircle className="h-3.5 w-3.5 text-muted-foreground" />;
  return null;
}

const EXAMPLES = [
  "Stage the raw tables, then build a marts table of monthly revenue by product category.",
  "Build a customer 360 pipeline: clean customers/orders, compute lifetime value, segment by spend.",
  "Design a full analytics warehouse: stage every source, build reusable intermediate models (order economics, customer activity, product performance, event funnels), then several marts — customer 360, monthly revenue by category, cohort retention, and a product KPI table — reusing shared intermediates across branches.",
];

// ---- layered DAG layout ---------------------------------------------------
const NODE_W = 210;
const NODE_H = 76;
const COL_GAP = 110;
const ROW_GAP = 34;
const PAD = 36;
const HEADER_H = 44;

type Placed = { node: DbtDagNode; x: number; y: number; col: number };

// ---- pipeline history (persisted across page switches / reloads) ----------
const HISTORY_KEY = "daclaw.etl.history.v1";
const MAX_HISTORY = 20;
// cross-page handoff: tables the Data Analysis page should preselect
export const DA_PRESELECT_KEY = "daclaw.dataAgent.preselect";

type PipelineRun = { id: string; project: string; goal: string; at: number; result: DbtBuildResponse };

function loadHistory(): PipelineRun[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    return raw ? (JSON.parse(raw) as PipelineRun[]) : [];
  } catch {
    return [];
  }
}
function persistHistory(history: PipelineRun[]) {
  try { localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(0, MAX_HISTORY))); } catch { /* quota */ }
}

// ---- module-level pipeline store -----------------------------------------
// Build state lives OUTSIDE the component so an in-flight pipeline (and its
// result) survives when the user navigates away from /etl and back. If this
// lived in component state, unmounting would turn the build's setResult into a
// no-op and the DAG would vanish on return — exactly the bug being fixed.
type EtlState = {
  goal: string;
  building: boolean;
  result: DbtBuildResponse | null;
  error: string | null;
  selected: string | null;
  history: PipelineRun[];
};
let etlState: EtlState = {
  goal: "",
  building: false,
  result: null,
  error: null,
  selected: null,
  history: loadHistory(),
};
const etlListeners = new Set<() => void>();
function setEtl(patch: Partial<EtlState>) {
  etlState = { ...etlState, ...patch };
  etlListeners.forEach((l) => l());
}
function subscribeEtl(cb: () => void) {
  etlListeners.add(cb);
  return () => { etlListeners.delete(cb); };
}
// Monotonic build token: a newer build supersedes an older one still in flight.
let buildSeq = 0;
async function runBuild(text: string) {
  if (!text || etlState.building) return;
  const seq = ++buildSeq;
  setEtl({ goal: text, building: true, error: null, selected: null });
  try {
    const r = await api.dbtBuild(text);
    if (seq !== buildSeq) return; // a newer build started; drop this result
    if (r.ok) {
      const run: PipelineRun = {
        id: `p_${Date.now().toString(36)}`,
        project: r.project || "pipeline",
        goal: text,
        at: Date.now(),
        result: r,
      };
      const history = [run, ...etlState.history].slice(0, MAX_HISTORY);
      persistHistory(history);
      setEtl({ result: r, history, building: false });
    } else {
      setEtl({ error: r.error || "build failed", building: false });
    }
  } catch (e) {
    if (seq !== buildSeq) return;
    setEtl({ error: String(e), building: false });
  }
}

export default function EtlPage() {
  const { t } = useI18n();
  const T = t.da;
  const navigate = useNavigate();
  const { goal, building, result, error, selected, history } = useSyncExternalStore(subscribeEtl, () => etlState);
  const [dbDialogOpen, setDbDialogOpen] = useState(false);
  const [sources, setSources] = useState<DATableInfo[]>([]);
  const [justConnected, setJustConnected] = useState<string | null>(null);
  // History rail is open by default whenever there are past runs (only auto-hides when empty).
  const [historyOpen, setHistoryOpen] = useState(() => loadHistory().length > 0);
  // In-page chat: ask about a model right here (same engine as Data Analysis).
  const [chatOpen, setChatOpen] = useState(false);
  const [chatScope, setChatScope] = useState<string[]>([]);
  const [chatSeed, setChatSeed] = useState<string | undefined>(undefined);
  // Resizable right inspector column.
  const [inspectorWidth, setInspectorWidth] = useState(400);
  const [inspectorResizing, setInspectorResizing] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const loadSources = () => api.daTables().then((r) => setSources(r.tables)).catch(() => {});
  useEffect(() => { loadSources(); }, []);
  // when a build finishes (building flips false with a result), refresh sources
  useEffect(() => { if (!building && result) loadSources(); }, [building, result]);
  // keep the history rail open once runs exist
  useEffect(() => { if (history.length > 0) setHistoryOpen(true); }, [history.length]);

  // inspector column resize (drag handle)
  useEffect(() => {
    if (!inspectorResizing) return;
    const onMove = (e: MouseEvent) => setInspectorWidth(Math.min(900, Math.max(300, window.innerWidth - e.clientX)));
    const onUp = () => setInspectorResizing(false);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [inspectorResizing]);

  const modelByName = useMemo(() => {
    const map = new Map<string, DbtModel>();
    (result?.models ?? []).forEach((m) => map.set(m.name, m));
    return map;
  }, [result]);

  // Lay out the DAG by TOPOLOGICAL DEPTH, not fixed layers. Depth = longest
  // path from any source, so complex non-strictly-layered trees (cross-layer
  // edges, skip-layer deps, many-to-many fan-in/out) render correctly. Each
  // depth becomes an X-column; nodes stack vertically within it. This replaces
  // the old rigid raw→staging→intermediate→marts 4-column assumption.
  const { placed, width, height, edges, colInfo } = useMemo(() => {
    const nodes = result?.dag?.nodes ?? [];
    const rawEdges = result?.dag?.edges ?? [];
    if (nodes.length === 0) {
      return { placed: new Map<string, Placed>(), width: 400, height: 320, edges: [] as { from: Placed; to: Placed }[], colInfo: [] as { col: number; x: number; count: number; layer: string }[] };
    }
    const byId = new Map(nodes.map((n) => [n.id, n]));
    // adjacency + indegree
    const outAdj = new Map<string, string[]>();
    const inAdj = new Map<string, string[]>();
    nodes.forEach((n) => { outAdj.set(n.id, []); inAdj.set(n.id, []); });
    for (const e of rawEdges) {
      if (byId.has(e.from) && byId.has(e.to)) {
        outAdj.get(e.from)!.push(e.to);
        inAdj.get(e.to)!.push(e.from);
      }
    }
    // longest-path depth via memoized DFS (graph is a DAG; guard cycles)
    const depth = new Map<string, number>();
    const visiting = new Set<string>();
    const computeDepth = (id: string): number => {
      if (depth.has(id)) return depth.get(id)!;
      if (visiting.has(id)) return 0; // cycle guard
      visiting.add(id);
      const preds = inAdj.get(id) ?? [];
      const d = preds.length === 0 ? 0 : 1 + Math.max(...preds.map(computeDepth));
      visiting.delete(id);
      depth.set(id, d);
      return d;
    };
    nodes.forEach((n) => computeDepth(n.id));

    // group by depth column
    const maxDepth = Math.max(0, ...nodes.map((n) => depth.get(n.id) ?? 0));
    const cols: DbtDagNode[][] = Array.from({ length: maxDepth + 1 }, () => []);
    // stable order within a column: by layer rank then name, keeps siblings tidy
    const layerRank = (l: string) => LAYER_ORDER.indexOf(l as never);
    nodes.forEach((n) => cols[depth.get(n.id) ?? 0].push(n));
    cols.forEach((list) => list.sort((a, b) => (layerRank(a.layer) - layerRank(b.layer)) || a.label.localeCompare(b.label)));

    const placedMap = new Map<string, Placed>();
    const maxRows = Math.max(1, ...cols.map((c) => c.length));
    const contentH = maxRows * NODE_H + Math.max(0, maxRows - 1) * ROW_GAP;
    cols.forEach((list, ci) => {
      const colH = list.length * NODE_H + Math.max(0, list.length - 1) * ROW_GAP;
      const startY = PAD + HEADER_H + (contentH - colH) / 2;
      list.forEach((node, ri) => {
        placedMap.set(node.id, {
          node,
          col: ci,
          x: PAD + ci * (NODE_W + COL_GAP),
          y: startY + ri * (NODE_H + ROW_GAP),
        });
      });
    });
    const nCols = cols.length;
    const w = PAD * 2 + nCols * NODE_W + Math.max(0, nCols - 1) * COL_GAP;
    const h = PAD * 2 + HEADER_H + contentH;
    // per-column band: label by the most common layer in that column
    const info = cols.map((list, ci) => {
      const counts: Record<string, number> = {};
      list.forEach((n) => { counts[n.layer] = (counts[n.layer] ?? 0) + 1; });
      const domLayer = Object.entries(counts).sort((a, b) => b[1] - a[1])[0]?.[0] ?? "marts";
      return { col: ci, x: PAD + ci * (NODE_W + COL_GAP), count: list.length, layer: domLayer };
    });
    const edgeList = rawEdges
      .map((e) => ({ from: placedMap.get(e.from), to: placedMap.get(e.to) }))
      .filter((e) => e.from && e.to) as { from: Placed; to: Placed }[];
    return { placed: placedMap, width: w, height: Math.max(h, 320), edges: edgeList, colInfo: info };
  }, [result]);

  const build = (g?: string) => {
    const text = (g ?? goal).trim();
    if (!text || building) return;
    runBuild(text);
  };

  const openRun = (run: PipelineRun) => {
    setEtl({ result: run.result, goal: run.goal, selected: null, error: null });
  };
  const deleteRun = (id: string) => {
    const history = etlState.history.filter((r) => r.id !== id);
    persistHistory(history);
    setEtl({ history });
  };

  // Primary: analyze a model right here, in-page (same engine as Data Analysis).
  // Guard: the model's table must actually be LIVE in the current engine. After a
  // backend restart the in-memory engine resets, so a model from a persisted
  // pipeline may no longer be materialized — in that case rebuild it first, then
  // open the chat, so the agent never queries a table that "does not exist".
  const [rematerializing, setRematerializing] = useState(false);
  const analyzeHere = async (modelName: string, seed?: string) => {
    const live = await api.daTables().then((r) => r.tables.some((t) => t.table === modelName)).catch(() => false);
    if (!live && goal.trim()) {
      // rebuild the pipeline to re-materialize the model tables
      setRematerializing(true);
      try {
        await runBuild(goal.trim());
      } finally {
        setRematerializing(false);
      }
      loadSources();
    }
    setChatScope([modelName]);
    setChatSeed(seed);
    setChatOpen(true);
  };
  // Secondary/alternative: hand off to the full Data Analysis page (preselected).
  const openInDataAnalysis = (modelName: string) => {
    try {
      localStorage.setItem(DA_PRESELECT_KEY, JSON.stringify({ tables: [modelName], at: Date.now() }));
    } catch { /* ignore */ }
    navigate("/data-agent");
  };

  const selectedModel = selected ? modelByName.get(selected) : null;

  return (
    <div className="mission-shell relative flex h-full flex-col overflow-hidden bg-[#07111f] text-slate-100">
      <ConnectDbDialog
        open={dbDialogOpen}
        onClose={() => setDbDialogOpen(false)}
        onConnected={(r) => {
          loadSources();
          setJustConnected(r.connected || r.dbtype || "database");
          setTimeout(() => setJustConnected(null), 4000);
        }}
      />
      {justConnected && (
        <div className="pointer-events-none absolute left-1/2 top-4 z-[70] -translate-x-1/2">
          <div className="flex items-center gap-2 rounded-full border border-emerald-300 bg-emerald-50 px-4 py-2 text-sm font-medium text-emerald-700 shadow-lg">
            <CheckCircle2 className="h-4 w-4" /> {T.connectedOk}: {justConnected}
          </div>
        </div>
      )}
      {/* header + NL input */}
      <div className="border-b border-white/5 bg-[#081321]/88 px-5 py-4 backdrop-blur-xl">
        <div className="mb-3 flex items-center gap-2">
          <span className="flex h-9 w-9 items-center justify-center rounded-xl border border-violet-300/20 bg-violet-300/10 text-violet-200"><Boxes className="h-5 w-5" /></span>
          <div><span className="text-sm font-black text-slate-50">{T.pipelineStudio}</span><div className="text-[10px] uppercase tracking-[.16em] text-slate-500">{T.etlSubtitle}</div></div>
          <button
            onClick={() => setHistoryOpen((v) => !v)}
            className={`ml-auto inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-medium ${
              historyOpen ? "border-[#2DD4BF] bg-[#2DD4BF]/10 text-[#2DD4BF]" : "border-border hover:bg-secondary"
            }`}
          >
            <History className="h-3.5 w-3.5" /> {T.etlHistory} {history.length > 0 && <span className="opacity-70">· {history.length}</span>}
          </button>
          <button
            onClick={() => setDbDialogOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-lg bg-[#2DD4BF] px-3 py-1.5 text-xs font-medium text-white hover:opacity-90"
          >
            <Plug className="h-3.5 w-3.5" /> {T.connectDb}
          </button>
        </div>
        <div className="mission-input flex items-end gap-2 rounded-2xl border border-white/7 bg-white/[0.025] p-2 shadow-[0_18px_60px_rgba(0,0,0,.2)]">
          <textarea
            value={goal}
            onChange={(e) => setEtl({ goal: e.target.value })}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) build();
            }}
            rows={2}
            placeholder={T.etlGoalPlaceholder}
            className="flex-1 resize-none border-0 bg-transparent px-3 py-2 text-sm text-slate-100 outline-none placeholder:text-slate-600"
          />
          <button
            onClick={() => build()}
            disabled={building || !goal.trim()}
            className="mission-primary inline-flex h-10 items-center gap-1.5 rounded-xl px-4 text-sm font-black text-[#041817] disabled:opacity-50"
          >
            {building ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
            {building ? T.etlBuilding : T.etlBuild}
          </button>
        </div>
        {!result && !building && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {EXAMPLES.map((ex) => (
              <button
                key={ex}
                onClick={() => build(ex)}
                className="rounded-full border border-border px-2.5 py-1 text-[11px] text-muted-foreground hover:bg-secondary hover:text-foreground"
              >
                {ex.length > 54 ? ex.slice(0, 54) + "…" : ex}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* body: history rail + DAG canvas + inspector */}
      <div className="flex min-h-0 flex-1">
        {/* History rail — visible by default whenever runs exist */}
        {historyOpen && history.length > 0 && (
          <aside className="flex w-60 shrink-0 flex-col border-r border-border bg-card/60">
            <div className="flex items-center gap-1.5 border-b border-border px-3 py-2.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              <History className="h-3.5 w-3.5 text-[#2DD4BF]" /> {T.etlHistory}
              <span className="ml-auto rounded-full bg-secondary px-1.5 py-0.5 text-[10px] font-bold">{history.length}</span>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-2">
              {history.map((run) => {
                const isActive = result === run.result;
                return (
                  <div
                    key={run.id}
                    className={`group mb-1 flex items-center gap-2 rounded-lg border px-2 py-2 transition-colors ${
                      isActive ? "border-[#2DD4BF] bg-[#2DD4BF]/5" : "border-transparent hover:bg-secondary"
                    }`}
                  >
                    <button onClick={() => openRun(run)} className="min-w-0 flex-1 text-left">
                      <div className="flex items-center gap-1.5">
                        <Boxes className="h-3.5 w-3.5 shrink-0 text-[#2DD4BF]" />
                        <span className="min-w-0 flex-1 truncate text-xs font-semibold">{run.project}</span>
                        <span className="shrink-0 text-[10px] font-medium text-muted-foreground">{run.result.n_ok}/{run.result.n_models}</span>
                      </div>
                      <div className="mt-0.5 line-clamp-2 text-[10px] leading-tight text-muted-foreground">{run.goal}</div>
                    </button>
                    <button onClick={() => deleteRun(run.id)} className="shrink-0 text-muted-foreground opacity-0 transition-opacity hover:text-red-500 group-hover:opacity-100">
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                );
              })}
            </div>
          </aside>
        )}
        <div ref={scrollRef} className="mission-canvas min-w-0 flex-1 overflow-auto bg-[radial-gradient(circle,rgba(115,155,215,0.16)_1px,transparent_1px)] [background-size:22px_22px]">
          {building && (
            <div className="flex h-full flex-col items-center justify-center gap-3 text-muted-foreground">
              <Loader2 className="h-8 w-8 animate-spin text-[#2DD4BF]" />
              <div className="text-sm">{T.etlBuildingHint}</div>
            </div>
          )}
          {!building && error && (
            <div className="m-6 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-600">
              {T.etlFailed}: {error}
            </div>
          )}
          {!building && !result && !error && (
            <div className="flex h-full flex-col items-center justify-center gap-3 text-center text-muted-foreground">
              <Layers className="h-10 w-10 text-[#2DD4BF]" />
              <div className="font-semibold text-foreground">{T.etlEmptyTitle}</div>
              <p className="max-w-md text-sm">
                {T.etlEmptyBody}
              </p>
              {sources.length > 0 ? (
                <div className="mt-2 w-full max-w-lg rounded-xl border border-emerald-300/20 bg-emerald-300/5 p-3 text-left shadow-[0_20px_70px_rgba(0,0,0,.18)]">
                  <div className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold text-emerald-300">
                    <CheckCircle2 className="h-3.5 w-3.5" /> {sources.length} {T.etlConnectedTables}
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {sources.map((t) => (
                      <span key={t.table} className="inline-flex items-center gap-1 rounded-md border border-border bg-card px-2 py-0.5 font-mono text-[11px]">
                        <Database className="h-3 w-3 text-[#2DD4BF]" />{t.table}
                        {t.row_count != null && <span className="text-muted-foreground">· {t.row_count.toLocaleString()}</span>}
                      </span>
                    ))}
                  </div>
                </div>
              ) : (
                <button
                  onClick={() => setDbDialogOpen(true)}
                  className="mt-1 inline-flex items-center gap-1.5 rounded-lg bg-[#2DD4BF] px-4 py-2 text-sm font-medium text-white hover:opacity-90"
                >
                  <Plug className="h-4 w-4" /> {T.connectDb}
                </button>
              )}
            </div>
          )}
          {!building && result && (
            <div className="relative min-h-full" style={{ width, height: Math.max(height, 400), minWidth: "100%" }}>
              {/* depth-column background bands */}
              {colInfo.map((c) => (
                <div
                  key={"band-" + c.col}
                  className="absolute rounded-2xl"
                  style={{
                    left: c.x - 14, top: PAD - 6, width: NODE_W + 28, bottom: PAD - 6,
                    background: `linear-gradient(180deg, ${LAYER_COLOR[c.layer]}0F, ${LAYER_COLOR[c.layer]}05)`,
                    border: `1px solid ${LAYER_COLOR[c.layer]}22`,
                  }}
                />
              ))}
              {/* depth-column headers (pill) */}
              {colInfo.map((c) => (
                <div
                  key={"hdr-" + c.col}
                  className="absolute flex items-center gap-1.5"
                  style={{ left: c.x, top: PAD, width: NODE_W }}
                >
                  <span
                    className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-bold uppercase tracking-wide text-white"
                    style={{ background: LAYER_COLOR[c.layer] }}
                  >
                    {c.col === 0 ? layerLabel("raw", T) : layerLabel(c.layer, T)}
                  </span>
                  <span className="text-[11px] font-medium text-muted-foreground">{c.count}</span>
                </div>
              ))}

              {/* edges */}
              <svg className="pointer-events-none absolute inset-0" width={width} height={Math.max(height, 400)}>
                <defs>
                  <linearGradient id="edge-grad" x1="0" y1="0" x2="1" y2="0">
                    <stop offset="0%" stopColor="#94a3b8" stopOpacity="0.5" />
                    <stop offset="100%" stopColor="#2DD4BF" stopOpacity="0.8" />
                  </linearGradient>
                  <marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
                    <path d="M0,0 L6,3 L0,6 Z" fill="#2DD4BF" />
                  </marker>
                  <marker id="arrow-active" markerWidth="9" markerHeight="9" refX="6" refY="3" orient="auto">
                    <path d="M0,0 L6,3 L0,6 Z" fill="#0EA5A3" />
                  </marker>
                </defs>
                {edges.map((e, i) => {
                  const x1 = e.from.x + NODE_W;
                  const y1 = e.from.y + NODE_H / 2;
                  const x2 = e.to.x - 6;
                  const y2 = e.to.y + NODE_H / 2;
                  const mx = (x1 + x2) / 2;
                  const active = !!selected && (e.from.node.id === selected || e.to.node.id === selected);
                  return (
                    <path
                      key={i}
                      d={`M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`}
                      fill="none"
                      stroke={active ? "#0EA5A3" : "url(#edge-grad)"}
                      strokeWidth={active ? 2.6 : 1.8}
                      markerEnd={active ? "url(#arrow-active)" : "url(#arrow)"}
                      className={active ? "" : "etl-edge"}
                    />
                  );
                })}
              </svg>

              {/* nodes */}
              {Array.from(placed.values()).map((p) => {
                const m = modelByName.get(p.node.id);
                const color = LAYER_COLOR[p.node.layer] ?? "#94a3b8";
                const isSel = selected === p.node.id;
                const isSource = p.node.kind === "source";
                return (
                  <button
                    key={p.node.id}
                    onClick={() => setEtl({ selected: isSel ? null : p.node.id })}
                    className={`group absolute flex flex-col overflow-hidden rounded-xl border bg-card text-left shadow-sm transition-all hover:-translate-y-0.5 hover:shadow-lg ${
                      isSel ? "ring-2 ring-offset-1" : "border-border"
                    }`}
                    style={{ left: p.x, top: p.y, width: NODE_W, height: NODE_H, ...(isSel ? { borderColor: color, boxShadow: `0 0 0 2px ${color}` } : {}) }}
                  >
                    {/* colored accent bar */}
                    <div className="h-1 w-full shrink-0" style={{ background: color }} />
                    <div className="flex min-h-0 flex-1 items-center gap-2 px-3">
                      <span
                        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-white"
                        style={{ background: isSource ? "#94a3b8" : color }}
                      >
                        {isSource ? <Database className="h-4 w-4" /> : <Boxes className="h-4 w-4" />}
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1">
                          <span className="min-w-0 flex-1 truncate font-mono text-xs font-semibold">{p.node.label}</span>
                          {m && <StatusIcon status={m.status} />}
                        </div>
                        <div className="mt-0.5 flex items-center gap-1.5 text-[10px] text-muted-foreground">
                          {m?.row_count != null ? (
                            <>
                              <span className="font-medium text-foreground">{m.row_count.toLocaleString()}</span>
                              <span>rows</span>
                              {m.materialized && (
                                <span className="rounded bg-secondary px-1 py-0.5 uppercase tracking-wide" style={{ color }}>{m.materialized}</span>
                              )}
                            </>
                          ) : (
                            <span>raw source</span>
                          )}
                        </div>
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        {/* right inspector (drag-resizable) */}
        {result && (
          <>
            <div onMouseDown={() => setInspectorResizing(true)} className="group relative z-20 flex h-full w-1.5 shrink-0 cursor-col-resize items-center justify-center">
              <div className="h-full w-px bg-border group-hover:bg-[#2DD4BF]" />
            </div>
            <aside style={{ width: inspectorWidth }} className="flex shrink-0 flex-col border-l border-border bg-card">
              {selectedModel ? (
                <ModelInspector
                  model={selectedModel}
                  onClose={() => setEtl({ selected: null })}
                  onAnalyzeHere={(name) => analyzeHere(name)}
                  onOpenInDA={openInDataAnalysis}
                  T={T}
                />
              ) : (
                <PipelineSummary result={result} onPick={(n) => setEtl({ selected: n })} T={T} />
              )}
            </aside>
          </>
        )}
      </div>

      {/* Re-materializing the pipeline (engine was reset) before opening chat */}
      {rematerializing && (
        <div className="absolute inset-0 z-[85] flex items-center justify-center bg-background/60 backdrop-blur-sm">
          <div className="flex items-center gap-3 rounded-xl border border-border bg-card px-5 py-4 shadow-xl">
            <Loader2 className="h-5 w-5 animate-spin text-[#2DD4BF]" />
            <div className="text-sm font-medium">{T.etlRematerializing}</div>
          </div>
        </div>
      )}

      {/* In-page chat overlay — ask about a model right here (same as Data Analysis) */}
      {chatOpen && (
        <div className="absolute inset-0 z-[80] flex items-stretch bg-background/40 backdrop-blur-sm" onClick={() => setChatOpen(false)}>
          <div className="ml-auto h-full w-[64%] min-w-[520px] max-w-[1000px] border-l border-border bg-card shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <EtlChatPanel scopeTables={chatScope} seedQuestion={chatSeed} onClose={() => setChatOpen(false)} />
          </div>
        </div>
      )}
    </div>
  );
}

function PipelineSummary({ result, onPick, T }: { result: DbtBuildResponse; onPick: (n: string) => void; T: DaT }) {
  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-border px-4 py-3">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <Boxes className="h-4 w-4 text-[#2DD4BF]" /> {result.project}
        </div>
        <div className="mt-1 text-xs text-muted-foreground">
          {result.n_ok}/{result.n_models} {T.etlModelsBuilt}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4">
        {result.explanation && <p className="mb-3 text-sm text-muted-foreground">{result.explanation}</p>}
        <div className="mb-3 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{T.etlModels}</div>
        <div className="space-y-1">
          {(result.models ?? []).map((m) => (
            <button
              key={m.name}
              onClick={() => onPick(m.name)}
              className="flex w-full items-center gap-2 rounded-md border border-border px-2.5 py-1.5 text-left text-xs hover:bg-secondary"
            >
              <span
                className="rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase text-white"
                style={{ background: LAYER_COLOR[m.layer] ?? "#94a3b8" }}
              >
                {m.layer.slice(0, 3)}
              </span>
              <span className="min-w-0 flex-1 truncate font-mono font-semibold">{m.name}</span>
              {m.row_count != null && <span className="text-muted-foreground">{m.row_count.toLocaleString()}</span>}
              <StatusIcon status={m.status} />
            </button>
          ))}
        </div>
      </div>
      {result.export && (
        <div className="border-t border-border p-3 text-[11px] text-muted-foreground">
          <div className="flex items-center gap-1.5">
            <Download className="h-3.5 w-3.5" />
            {T.etlExported} · {result.export.n_files} {T.etlFiles}
          </div>
          <div className="mt-1 break-all font-mono text-[10px]">{result.export.root}</div>
        </div>
      )}
    </div>
  );
}

function ModelInspector({ model, onClose, onAnalyzeHere, onOpenInDA, T }: { model: DbtModel; onClose: () => void; onAnalyzeHere: (name: string) => void; onOpenInDA: (name: string) => void; T: DaT }) {
  const canAnalyze = model.status === "ok";
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-border px-4 py-3">
        <span
          className="rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase text-white"
          style={{ background: LAYER_COLOR[model.layer] ?? "#94a3b8" }}
        >
          {model.layer}
        </span>
        <span className="min-w-0 flex-1 truncate font-mono text-sm font-semibold">{model.name}</span>
        <StatusIcon status={model.status} />
        <button onClick={onClose} className="rounded p-1 text-muted-foreground hover:bg-secondary">
          <X className="h-4 w-4" />
        </button>
      </div>
      {canAnalyze && (
        <div className="border-b border-border px-4 py-2.5">
          {/* Primary: analyze in-page. Secondary: hand off to the full page. */}
          <button
            onClick={() => onAnalyzeHere(model.name)}
            className="flex w-full items-center justify-center gap-1.5 rounded-lg bg-[#2DD4BF] px-3 py-2 text-sm font-medium text-white hover:opacity-90"
          >
            <Sparkles className="h-4 w-4" /> {T.etlAnalyzeHere}
          </button>
          <button
            onClick={() => onOpenInDA(model.name)}
            className="mt-1.5 flex w-full items-center justify-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground hover:bg-secondary hover:text-foreground"
          >
            <ArrowUpRight className="h-3.5 w-3.5" /> {T.etlOpenInDA}
          </button>
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-auto p-4">
        {model.description && <p className="mb-3 text-sm text-muted-foreground">{model.description}</p>}
        {model.error && (
          <div className="mb-3 rounded-md border border-red-200 bg-red-50 p-2 text-xs text-red-600">{model.error}</div>
        )}
        <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          SQL {model.materialized ? `· ${model.materialized}` : ""}
        </div>
        <SqlCode sql={model.sql || ""} className="mb-4" />
        {model.status === "ok" && model.columns && model.preview && (
          <>
            <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              {T.etlPreview} · {model.row_count?.toLocaleString()} {T.rows}
            </div>
            <div className="overflow-x-auto rounded-lg border border-border">
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="border-b border-border bg-secondary/50">
                    {model.columns.map((c) => (
                      <th key={c} className="px-2 py-1 text-left font-semibold">{c}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {model.preview.map((row, ri) => (
                    <tr key={ri} className="border-b border-border last:border-0">
                      {(row as unknown[]).map((v, ci) => (
                        <td key={ci} className="px-2 py-1 tabular-nums">
                          {v == null ? <span className="text-muted-foreground">·</span> : String(v)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
