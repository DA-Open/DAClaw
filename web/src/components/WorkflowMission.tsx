import { useMemo, useState } from "react";
import {
  Braces,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Circle,
  Database,
  FileSearch,
  Gauge,
  Loader2,
  Network,
  ShieldCheck,
  Sparkles,
  X,
} from "lucide-react";
import type { ChatTraceEvent, DAWorkflow, DAWorkflowNode } from "@/lib/api";
import { useI18n } from "@/i18n";
import type { Translations } from "@/i18n";

type DaT = Translations["da"];

const KIND_ICON: Record<string, typeof Database> = {
  source_read: Database,
  knowledge_search: FileSearch,
  profile: Gauge,
  query: Braces,
  visualize: Sparkles,
  dashboard: Gauge,
  report: FileSearch,
  excel: Braces,
  dbt: Network,
  story: Sparkles,
};

function nodeState(node: DAWorkflowNode, trace: ChatTraceEvent[], running: boolean) {
  if (node.kind === "source_read" && node.meta?.table) return "completed";
  const starts = trace.filter((event) => event.type === "tool_start" && event.name === node.tool);
  const ends = trace.filter((event) => event.type === "tool_complete" && event.name === node.tool);
  const errors = trace.filter((event) => event.is_error && event.name === node.tool);
  if (errors.length > 0) return "error";
  if (ends.length > 0) return "completed";
  if (starts.length > ends.length) return "running";
  if (!running && trace.length > 0) return "skipped";
  return "planned";
}

function StateIcon({ state }: { state: string }) {
  if (state === "completed") return <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />;
  if (state === "running") return <Loader2 className="h-3.5 w-3.5 animate-spin text-cyan-300" />;
  if (state === "error") return <Circle className="h-3.5 w-3.5 fill-red-400 text-red-400" />;
  return <Circle className="h-3.5 w-3.5 text-slate-600" />;
}

// Pull the real SQL + output for a node out of the trace. Matching is by tool
// name (trace events carry no node_id), so for multiple same-tool nodes this
// returns the first start/complete pair — good enough for the inspector.
function nodeEvidence(node: DAWorkflowNode, trace: ChatTraceEvent[]) {
  const start = trace.find((e) => e.type === "tool_start" && e.name === node.tool);
  const done = trace.find((e) => e.type === "tool_complete" && e.name === node.tool);
  const sql = start?.args && typeof (start.args as Record<string, unknown>).sql === "string"
    ? (start.args as Record<string, unknown>).sql as string
    : null;
  const output = done?.result || done?.preview || start?.preview || "";
  const duration = done?.duration ?? null;
  return { sql, output, duration };
}

export function WorkflowMission({
  workflow,
  trace = [],
  running = false,
  editable = false,
  onRemoveNode,
}: {
  workflow: DAWorkflow;
  trace?: ChatTraceEvent[];
  running?: boolean;
  editable?: boolean;
  onRemoveNode?: (nodeId: string) => void;
}) {
  const { t } = useI18n();
  const T = t.da;
  const [open, setOpen] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const byId = useMemo(() => new Map(workflow.nodes.map((node) => [node.id, node])), [workflow]);
  const completed = workflow.nodes.filter((node) => nodeState(node, trace, running) === "completed").length;
  const progress = workflow.nodes.length ? Math.round((completed / workflow.nodes.length) * 100) : 0;
  const selected = selectedId ? byId.get(selectedId) : null;

  return (
    <div className="mission-workflow mb-2 overflow-hidden rounded-2xl border border-cyan-400/15 bg-[#212e42]/90 shadow-[0_18px_60px_rgba(3,10,25,.28)]">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-3 px-4 py-3 text-left"
      >
        <span className="flex h-8 w-8 items-center justify-center rounded-xl border border-cyan-300/25 bg-cyan-300/10 text-cyan-200">
          <Network className="h-4 w-4" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-black uppercase tracking-[0.2em] text-cyan-300">{T.compiledMission}</span>
            {workflow.validation.ok && (
              <span className="inline-flex items-center gap-1 rounded-full bg-emerald-400/10 px-2 py-0.5 text-[9px] font-bold text-emerald-300">
                <ShieldCheck className="h-3 w-3" /> {T.validated}
              </span>
            )}
          </div>
          <div className="mt-1 truncate text-xs text-slate-300">
            {workflow.nodes.length} nodes · {workflow.optimizer.critical_path_nodes} layers · max {workflow.optimizer.max_parallelism} parallel
          </div>
        </div>
        <div className="hidden w-32 items-center gap-2 sm:flex">
          <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-800">
            <div className="h-full rounded-full bg-gradient-to-r from-blue-500 via-cyan-300 to-emerald-300 transition-all" style={{ width: `${progress}%` }} />
          </div>
          <span className="w-8 text-right text-[10px] tabular-nums text-slate-400">{progress}%</span>
        </div>
        {open ? <ChevronDown className="h-4 w-4 text-slate-500" /> : <ChevronRight className="h-4 w-4 text-slate-500" />}
      </button>

      {open && (
        <div className="border-t border-white/5 px-4 pb-4 pt-3">
          <div className="overflow-x-auto pb-1">
            <div className="flex min-w-max items-stretch gap-3">
              {workflow.execution_layers.map((layer, layerIndex) => (
                <div key={layerIndex} className="relative min-w-[190px] rounded-xl border border-white/7 bg-white/[0.025] p-2.5">
                  <div className="mb-2 flex items-center justify-between text-[9px] font-black uppercase tracking-[0.16em] text-slate-500">
                    <span>{T.layer} {String(layerIndex + 1).padStart(2, "0")}</span>
                    {layer.length > 1 && <span className="text-cyan-400">{T.parallel} ×{layer.length}</span>}
                  </div>
                  <div className="space-y-2">
                    {(() => {
                      // Collapse same-label nodes in this layer into one card with
                      // a ×N badge so N identical "Read data source" steps don't
                      // stack into a noisy column.
                      const nodesInLayer = layer.map((id) => byId.get(id)).filter(Boolean) as typeof workflow.nodes;
                      const groups: { node: (typeof workflow.nodes)[number]; ids: string[] }[] = [];
                      for (const node of nodesInLayer) {
                        const key = `${node.label}|${node.tool}|${node.kind}`;
                        const prev = groups[groups.length - 1];
                        if (prev && `${prev.node.label}|${prev.node.tool}|${prev.node.kind}` === key) {
                          prev.ids.push(node.id);
                        } else {
                          groups.push({ node, ids: [node.id] });
                        }
                      }
                      return groups.map(({ node, ids }) => {
                      const count = ids.length;
                      const state = nodeState(node, trace, running);
                      const Icon = KIND_ICON[node.kind] ?? Circle;
                      const isSel = selectedId === node.id;
                      return (
                        <div key={node.id} className={`mission-node mission-node-${state} flex items-start gap-2 rounded-lg border px-2.5 py-2 ${isSel ? "ring-1 ring-cyan-300/60" : ""}`}>
                          <button type="button" onClick={() => setSelectedId(isSel ? null : node.id)} className="flex min-w-0 flex-1 items-start gap-2 text-left">
                            <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-white/5 text-cyan-200">
                              <Icon className="h-3.5 w-3.5" />
                            </span>
                            <div className="min-w-0 flex-1">
                              <div className="flex items-center gap-1">
                                <span className="truncate text-[11px] font-bold text-slate-100">{node.label}</span>
                                {count > 1 && <span className="shrink-0 rounded-full bg-white/10 px-1.5 text-[9px] font-black text-cyan-200">×{count}</span>}
                              </div>
                              <div className="mt-0.5 truncate font-mono text-[9px] text-slate-500">{node.tool}</div>
                            </div>
                          </button>
                          <StateIcon state={state} />
                          {editable && count === 1 && !["source_read", "query", "dbt"].includes(node.kind) && (
                            <button type="button" title={T.removeStep} onClick={() => onRemoveNode?.(node.id)} className="rounded p-0.5 text-slate-600 hover:bg-red-400/10 hover:text-red-300">
                              <X className="h-3 w-3" />
                            </button>
                          )}
                        </div>
                      );
                      });
                    })()}
                  </div>
                </div>
              ))}
            </div>
          </div>
          {workflow.validation.warnings.length > 0 && (
            <div className="mt-2 text-[10px] text-amber-300/80">{workflow.validation.warnings[0].message}</div>
          )}
          {selected && (
            <NodeInspector node={selected} state={nodeState(selected, trace, running)} evidence={nodeEvidence(selected, trace)} onClose={() => setSelectedId(null)} T={T} />
          )}
        </div>
      )}
    </div>
  );
}

// Per-node inspector: inputs (meta + description), the real SQL executed, live
// status, and an output preview — the DeepEye-style "node inspector".
function NodeInspector({
  node, state, evidence, onClose, T,
}: {
  node: DAWorkflowNode;
  state: string;
  evidence: { sql: string | null; output: string; duration: number | null };
  onClose: () => void;
  T: DaT;
}) {
  const meta = node.meta && Object.keys(node.meta).length > 0 ? node.meta : null;
  const stateLabel: Record<string, string> = {
    completed: T.nodeCompleted, running: T.nodeRunning, error: T.nodeError,
    skipped: T.nodeSkipped, planned: T.nodePlanned,
  };
  return (
    <div className="mt-3 overflow-hidden rounded-xl border border-cyan-300/25 bg-[#1a2536]/80">
      <div className="flex items-center gap-2 border-b border-white/5 px-3 py-2">
        <span className="text-[10px] font-black uppercase tracking-[0.16em] text-cyan-300">{T.nodeInspector}</span>
        <span className="truncate text-xs font-bold text-slate-100">{node.label}</span>
        <span className="ml-auto rounded-full bg-white/5 px-2 py-0.5 text-[9px] font-bold text-slate-300">{stateLabel[state] ?? state}</span>
        <button type="button" onClick={onClose} className="rounded p-0.5 text-slate-500 hover:text-slate-200"><X className="h-3.5 w-3.5" /></button>
      </div>
      <div className="space-y-2.5 p-3 text-[11px]">
        {node.description && (
          <div>
            <div className="mb-0.5 text-[9px] font-bold uppercase tracking-wide text-slate-500">{T.nodeInputs}</div>
            <div className="text-slate-300">{node.description}</div>
          </div>
        )}
        {meta && (
          <div className="flex flex-wrap gap-1.5">
            {Object.entries(meta).map(([k, v]) => (
              <span key={k} className="rounded bg-white/5 px-1.5 py-0.5 font-mono text-[9px] text-slate-400">{k}: {String(v)}</span>
            ))}
          </div>
        )}
        {evidence.sql && (
          <div>
            <div className="mb-0.5 flex items-center gap-1.5 text-[9px] font-bold uppercase tracking-wide text-slate-500">
              {T.nodeSql}{evidence.duration != null && <span className="text-slate-600">· {evidence.duration.toFixed(1)}s</span>}
            </div>
            <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded-lg bg-[#0f172a] px-3 py-2 font-mono text-[10.5px] leading-relaxed text-slate-200">{evidence.sql}</pre>
          </div>
        )}
        {evidence.output && (
          <div>
            <div className="mb-0.5 text-[9px] font-bold uppercase tracking-wide text-slate-500">{T.nodeOutput}</div>
            <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-white/5 bg-white/[0.02] px-3 py-2 font-mono text-[10px] text-slate-400">{evidence.output}</pre>
          </div>
        )}
        {!evidence.sql && !evidence.output && (
          <div className="text-slate-500">{state === "planned" ? T.nodeNotRunYet : T.nodeNoOutput}</div>
        )}
      </div>
    </div>
  );
}
