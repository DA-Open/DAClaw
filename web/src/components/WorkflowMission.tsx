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
  const byId = useMemo(() => new Map(workflow.nodes.map((node) => [node.id, node])), [workflow]);
  const completed = workflow.nodes.filter((node) => nodeState(node, trace, running) === "completed").length;
  const progress = workflow.nodes.length ? Math.round((completed / workflow.nodes.length) * 100) : 0;

  return (
    <div className="mission-workflow mb-2 overflow-hidden rounded-2xl border border-cyan-400/15 bg-[#091526]/90 shadow-[0_18px_60px_rgba(3,10,25,.28)]">
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
                    {layer.map((nodeId) => {
                      const node = byId.get(nodeId);
                      if (!node) return null;
                      const state = nodeState(node, trace, running);
                      const Icon = KIND_ICON[node.kind] ?? Circle;
                      return (
                        <div key={nodeId} className={`mission-node mission-node-${state} flex items-start gap-2 rounded-lg border px-2.5 py-2`}>
                          <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-white/5 text-cyan-200">
                            <Icon className="h-3.5 w-3.5" />
                          </span>
                          <div className="min-w-0 flex-1">
                            <div className="truncate text-[11px] font-bold text-slate-100">{node.label}</div>
                            <div className="mt-0.5 truncate font-mono text-[9px] text-slate-500">{node.tool}</div>
                          </div>
                          <StateIcon state={state} />
                          {editable && !["source_read", "query", "dbt"].includes(node.kind) && (
                            <button type="button" title={T.removeStep} onClick={() => onRemoveNode?.(node.id)} className="rounded p-0.5 text-slate-600 hover:bg-red-400/10 hover:text-red-300">
                              <X className="h-3 w-3" />
                            </button>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          </div>
          {workflow.validation.warnings.length > 0 && (
            <div className="mt-2 text-[10px] text-amber-300/80">{workflow.validation.warnings[0].message}</div>
          )}
        </div>
      )}
    </div>
  );
}
