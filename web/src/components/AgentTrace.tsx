import { useState } from "react";
import { ChevronDown, ChevronRight, Loader2, Copy, Check } from "lucide-react";
import type { ChatTraceEvent } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useI18n } from "@/i18n";
import type { Translations } from "@/i18n";

type DaT = Translations["da"];

// ---------------------------------------------------------------------------
// AgentTrace — the "ran this SQL" execution trace for the Data Agent chat.
// Extracted from ChatPage's trace UI and extended with a `sourceMode` prop:
//   - "database": SQL from data_query/data_visualize is shown prominently in a
//     dedicated "Ran this SQL" card (copyable).
//   - "table":    same data, but SQL is de-emphasized (collapsed under a neutral
//     "Analyzed the table" label) since the source is an Excel/CSV sheet.
// ---------------------------------------------------------------------------

const TRACE_EVENT_LIMIT = 60;
const TRACE_TEXT_LIMIT = 3000;

export type SourceMode = "database" | "table";

const SQL_TOOLS = new Set(["data_query", "data_visualize"]);

function truncateText(value: string | undefined, limit = TRACE_TEXT_LIMIT): string | undefined {
  if (value === undefined || value === null) return value;
  if (value.length <= limit) return value;
  return `${value.slice(0, limit)}\n\n... [truncated]`;
}

function sanitizeTraceEvent(event: ChatTraceEvent): ChatTraceEvent {
  let args = event.args;
  if (args && JSON.stringify(args).length > TRACE_TEXT_LIMIT) {
    args = { _truncated: truncateText(JSON.stringify(args), TRACE_TEXT_LIMIT) };
  }
  return {
    ...event,
    message: truncateText(event.message),
    text: truncateText(event.text),
    preview: truncateText(event.preview),
    result: truncateText(event.result),
    args,
    // artifact is a small structured object — keep it verbatim.
  };
}

export function shouldKeepTraceEvent(event: ChatTraceEvent): boolean {
  if (
    event.type === "assistant_delta" ||
    event.type === "reasoning" ||
    event.type === "step" ||
    event.type === "run_started"
  ) {
    return false;
  }
  if (event.type === "status") {
    return Boolean(event.is_error || /error|failed|exception|timeout/i.test(event.message || ""));
  }
  if (event.type === "tool_progress") {
    return Boolean(event.is_error);
  }
  return event.type === "tool_start" || event.type === "tool_complete" || event.type === "error";
}

export function appendTraceEvent(trace: ChatTraceEvent[] = [], event: ChatTraceEvent): ChatTraceEvent[] {
  if (!shouldKeepTraceEvent(event)) return trace;
  return [...trace, sanitizeTraceEvent(event)].slice(-TRACE_EVENT_LIMIT);
}

export function sanitizeTrace(trace: ChatTraceEvent[] = []): ChatTraceEvent[] {
  return trace.filter(shouldKeepTraceEvent).map(sanitizeTraceEvent).slice(-TRACE_EVENT_LIMIT);
}

function extractSql(event: ChatTraceEvent): string | null {
  const a = event.args;
  if (a && typeof a === "object" && typeof (a as Record<string, unknown>).sql === "string") {
    return (a as Record<string, unknown>).sql as string;
  }
  return null;
}

export function TracePanel({
  trace = [],
  running = false,
  sourceMode = "database",
}: {
  trace?: ChatTraceEvent[];
  running?: boolean;
  sourceMode?: SourceMode;
}) {
  const [open, setOpen] = useState(running);
  const { t } = useI18n();
  const tr = t.da;
  const items = trace;
  const toolCount = items.filter((item) => item.type.includes("tool")).length;

  if (!running && items.length === 0) return null;

  return (
    <div className="mt-1 overflow-hidden rounded-2xl border border-border bg-muted/40">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-xs text-muted-foreground hover:bg-secondary"
        onClick={() => setOpen((value) => !value)}
      >
        {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        <span className="font-medium text-foreground">
          {sourceMode === "table" ? tr.traceAnalyze : tr.traceCall}
        </span>
        <span className="ml-auto">{running ? tr.traceRunning : `${toolCount} ${tr.traceSteps}`}</span>
      </button>
      {open && (
        <div className="border-t border-border">
          <div className="divide-y divide-border">
            {items.map((event, index) => (
              <TraceEvent key={`${event.type}-${index}`} event={event} sourceMode={sourceMode} tr={tr} />
            ))}
            {running && <TraceActiveRow trace={trace} visibleItems={items} sourceMode={sourceMode} tr={tr} />}
          </div>
        </div>
      )}
    </div>
  );
}

function TraceActiveRow({
  trace,
  visibleItems,
  sourceMode,
  tr,
}: {
  trace: ChatTraceEvent[];
  visibleItems: ChatTraceEvent[];
  sourceMode: SourceMode;
  tr: DaT;
}) {
  const lastVisible = visibleItems[visibleItems.length - 1];
  const lastRaw = trace[trace.length - 1];
  let label = tr.tStarting;

  if (lastVisible?.type === "tool_start") {
    const isTableSql = sourceMode === "table" && SQL_TOOLS.has(lastVisible.name || "");
    const verb = isTableSql ? tr.tAnalyzeTable : lastVisible.summary || lastVisible.name || "tool";
    label = isTableSql ? `${tr.tRunning} ${verb}` : `${tr.tRunning} ${verb}`;
  } else if (lastRaw?.type === "assistant_delta") {
    label = tr.tWriting;
  } else if (lastVisible?.type === "tool_complete") {
    label = tr.tAnalyzing;
  } else if (lastRaw?.type === "step" || lastRaw?.type === "run_started") {
    label = tr.tPlanning;
  }

  return (
    <div className="flex items-center gap-3 px-4 py-2.5 text-xs text-muted-foreground">
      <span className="w-3">
        <Loader2 className="h-3 w-3 animate-spin" />
      </span>
      <span className="rounded-full bg-background px-2 py-0.5 text-[0.68rem] font-medium">{tr.inProgress}</span>
      <span className="min-w-0 flex-1 truncate text-foreground">{label}</span>
    </div>
  );
}

function SqlCard({ sql, emphasized, tr }: { sql: string; emphasized: boolean; tr: DaT }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(sql).then(
      () => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1200);
      },
      () => {},
    );
  };
  return (
    <div
      className={cn(
        "overflow-hidden rounded-lg border",
        emphasized ? "border-[#2DD4BF]/40 bg-[#2DD4BF]/5" : "border-border bg-muted/40",
      )}
    >
      <div className="flex items-center gap-2 px-3 py-1.5 text-[0.68rem]">
        <span className={cn("font-semibold", emphasized ? "text-[#2DD4BF]" : "text-muted-foreground")}>
          {emphasized ? tr.ranThisSql : tr.sqlLabel}
        </span>
        <button
          type="button"
          onClick={copy}
          className="ml-auto inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-muted-foreground hover:bg-secondary"
        >
          {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
          {copied ? tr.copied : tr.copy}
        </button>
      </div>
      <pre className="overflow-x-auto whitespace-pre-wrap break-words border-t border-border/60 bg-[#0f172a] px-3 py-2 font-mono-ui text-[0.72rem] text-slate-200">
        {sql}
      </pre>
    </div>
  );
}

function TraceEvent({ event, sourceMode, tr }: { event: ChatTraceEvent; sourceMode: SourceMode; tr: DaT }) {
  const isSqlTool = SQL_TOOLS.has(event.name || "");
  const sql = isSqlTool ? extractSql(event) : null;
  // In database mode, a SQL card is prominent → default open. In table mode,
  // keep it collapsed so SQL is available but not front-and-center.
  const [open, setOpen] = useState(
    Boolean(event.is_error || event.type === "error" || (sql && sourceMode === "database" && event.type === "tool_start")),
  );
  const label = formatTraceLabel(event, sourceMode, tr);
  const meta = event.duration ? `${event.duration.toFixed(1)}s` : event.is_error ? tr.stError : "";
  const otherArgs =
    event.args && Object.keys(event.args).filter((k) => k !== "sql").length > 0
      ? Object.fromEntries(Object.entries(event.args).filter(([k]) => k !== "sql"))
      : null;
  const hasBody = Boolean(
    event.text || event.message || event.preview || event.result || otherArgs || sql,
  );

  return (
    <div>
      <button
        type="button"
        className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-xs hover:bg-secondary/80 disabled:cursor-default"
        onClick={() => hasBody && setOpen((value) => !value)}
        disabled={!hasBody}
      >
        {hasBody ? (
          open ? (
            <ChevronDown className="h-3 w-3 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-3 w-3 text-muted-foreground" />
          )
        ) : (
          <span className="w-3" />
        )}
        <span
          className={cn(
            "rounded-full px-2 py-0.5 text-[0.68rem] font-medium",
            event.type === "tool_complete" ? "bg-emerald-50 text-emerald-700" : "bg-background text-muted-foreground",
            (event.is_error || event.type === "error") && "bg-destructive/10 text-destructive",
          )}
        >
          {traceStatusLabel(event, tr)}
        </span>
        <span className="min-w-0 flex-1 truncate text-foreground">{label}</span>
        {meta && <span className="text-muted-foreground">{meta}</span>}
      </button>
      {open && hasBody && (
        <div className="space-y-2 px-6 pb-3 text-xs">
          {sql && <SqlCard sql={sql} emphasized={sourceMode === "database"} tr={tr} />}
          {event.message && <TraceText label="status" value={event.message} />}
          {event.preview && <TraceText label="preview" value={event.preview} />}
          {otherArgs && <TraceText label="args" value={JSON.stringify(otherArgs, null, 2)} mono />}
          {event.result && <TraceText label="result" value={event.result} mono />}
        </div>
      )}
    </div>
  );
}

function formatTraceLabel(event: ChatTraceEvent, sourceMode: SourceMode, tr: DaT): string {
  const name = event.name || event.event || "DAClaw";
  const isSqlTool = SQL_TOOLS.has(name);
  // In table mode, de-emphasize SQL by using neutral analysis verbs.
  if (sourceMode === "table" && isSqlTool) {
    if (event.type === "tool_start") return name === "data_visualize" ? tr.tGenChart : tr.tAnalyzeTable;
    if (event.type === "tool_complete") return name === "data_visualize" ? tr.tChartDone : tr.tAnalyzeDone;
  }
  const target = event.summary || name;
  if (event.type === "tool_start") return `${tr.tRun} ${target}`;
  if (event.type === "tool_complete") return `${tr.tDone} ${target}`;
  if (event.type === "tool_progress") return `${target} ${tr.tProblem}`;
  if (event.type === "error") return tr.tFailed;
  return event.message || name;
}

function traceStatusLabel(event: ChatTraceEvent, tr: DaT): string {
  if (event.type === "tool_start") return tr.stCall;
  if (event.type === "tool_complete") return tr.stDone;
  if (event.type === "tool_progress") return tr.stTool;
  if (event.type === "error" || event.is_error) return tr.stError;
  return tr.stStatus;
}

function TraceText({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="grid gap-1 sm:grid-cols-[5rem_1fr]">
      <span className="text-[0.65rem] uppercase tracking-wide text-muted-foreground">{label}</span>
      <pre className={cn("whitespace-pre-wrap break-words text-muted-foreground", mono && "font-mono-ui")}>{value}</pre>
    </div>
  );
}
