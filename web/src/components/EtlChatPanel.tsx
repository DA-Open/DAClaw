import { useEffect, useRef, useState } from "react";
import { Send, Loader2, Square, User, Download, X, BarChart3, FileText, FileSpreadsheet, Sparkles, Table2 } from "lucide-react";
import { api } from "@/lib/api";
import type { ChatRunEvent, ChatTraceEvent, DAArtifact } from "@/lib/api";
import { Markdown } from "@/components/Markdown";
import { TracePanel, appendTraceEvent, sanitizeTrace } from "@/components/AgentTrace";
import { BotAvatar } from "@/components/BotAvatar";
import { useI18n } from "@/i18n";

// A compact, self-contained data-agent chat used INSIDE the Data Engineering
// page so the user can ask questions about a pipeline model right there — same
// engine and artifacts as the Data Analysis page, no navigation required.
// Single conversation (no multi-conversation store); scoped to `scopeTables`.

type ChatMsg = { role: "user" | "assistant"; content: string; trace?: ChatTraceEvent[]; running?: boolean };

function delay(ms: number) { return new Promise((r) => setTimeout(r, ms)); }
function isTerminal(s?: string) { return s === "completed" || s === "failed" || s === "cancelled"; }
function artifactIcon(kind: string) {
  if (kind === "chart") return <BarChart3 className="h-4 w-4 text-[#2DD4BF]" />;
  if (kind === "dashboard") return <Table2 className="h-4 w-4 text-[#6366F1]" />;
  if (kind === "excel") return <FileSpreadsheet className="h-4 w-4 text-emerald-500" />;
  return <FileText className="h-4 w-4 text-[#F59E0B]" />;
}

export function EtlChatPanel({
  scopeTables,
  onClose,
  seedQuestion,
}: {
  scopeTables: string[];
  onClose: () => void;
  seedQuestion?: string;
}) {
  const { t } = useI18n();
  const T = t.da;
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [artifacts, setArtifacts] = useState<DAArtifact[]>([]);
  const [activeArtifactId, setActiveArtifactId] = useState<string | null>(null);
  const [artifactHtml, setArtifactHtml] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const deltaBufferRef = useRef("");
  const deltaTimerRef = useRef<number | null>(null);

  const artifact = artifacts.find((a) => a.file_id === activeArtifactId) ?? null;

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  // fetch preview HTML for the active artifact
  useEffect(() => {
    if (!artifact) { setArtifactHtml(null); return; }
    let cancelled = false;
    setArtifactHtml(null);
    (async () => {
      try {
        const data = await api.getWorkspaceFilePreview(artifact.file_id);
        if (cancelled) return;
        if (data.preview?.type === "html") setArtifactHtml(data.preview.html);
      } catch { /* download-only */ }
    })();
    return () => { cancelled = true; };
  }, [artifact?.file_id]);

  const flushDelta = (idx: number) => {
    const d = deltaBufferRef.current;
    if (!d) return;
    deltaBufferRef.current = "";
    setMessages((m) => m.map((msg, i) => (i === idx ? { ...msg, content: msg.content + d } : msg)));
  };
  const scheduleDelta = (idx: number, d: string) => {
    deltaBufferRef.current += d;
    if (deltaTimerRef.current !== null) return;
    deltaTimerRef.current = window.setTimeout(() => { deltaTimerRef.current = null; flushDelta(idx); }, 120);
  };
  const addArtifact = (art: DAArtifact) => {
    setArtifacts((a) => (a.some((x) => x.file_id === art.file_id) ? a.map((x) => (x.file_id === art.file_id ? art : x)) : [...a, art]));
    setActiveArtifactId(art.file_id);
  };

  const applyEvent = (idx: number, event: ChatRunEvent) => {
    if (event.type === "assistant_delta" && "delta" in event) { scheduleDelta(idx, event.delta); return; }
    if (event.type === "heartbeat") return;
    if (event.type === "tool_complete" && (event as ChatTraceEvent).artifact) addArtifact((event as ChatTraceEvent).artifact!);
    if (event.type === "done" && "final_response" in event) {
      if (deltaTimerRef.current !== null) { window.clearTimeout(deltaTimerRef.current); deltaTimerRef.current = null; }
      flushDelta(idx);
      (event.trace || []).filter((tr) => tr.artifact).forEach((tr) => addArtifact(tr.artifact!));
      setMessages((m) => m.map((msg, i) => (i === idx ? { ...msg, content: event.final_response || msg.content, trace: sanitizeTrace(event.trace), running: false } : msg)));
      return;
    }
    if (event.type === "error") {
      setMessages((m) => m.map((msg, i) => (i === idx ? { ...msg, content: msg.content || `Request failed: ${event.message}`, running: false, trace: appendTraceEvent(msg.trace, event) } : msg)));
      return;
    }
    setMessages((m) => m.map((msg, i) => (i === idx ? { ...msg, trace: appendTraceEvent(msg.trace, event) } : msg)));
  };

  const send = async (explicit?: string) => {
    const text = (explicit ?? input).trim();
    if (!text || busy) return;
    setInput("");
    setBusy(true);
    const history = messages.map((m) => ({ role: m.role, content: m.content }));
    let idx = 0;
    setMessages((m) => { idx = m.length + 1; return [...m, { role: "user", content: text }, { role: "assistant", content: "", running: true }]; });

    const scoped = scopeTables.length > 0
      ? `${text}\n\n[Scope: focus this request on the following table(s) in the connected database: ${scopeTables.join(", ")}. Query/analyze these unless I say otherwise.]`
      : text;

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const run = await api.startDaAgentRun({ message: scoped, history });
      let after = -1;
      while (true) {
        if (ctrl.signal.aborted) throw new DOMException("Aborted", "AbortError");
        const payload = await api.getChatRunEvents(run.run_id, after);
        for (const event of payload.events) { after = event.seq ?? after; applyEvent(idx, event); }
        if (isTerminal(payload.run.status)) break;
        await delay(payload.events.length > 0 ? 200 : 900);
      }
    } catch (e) {
      const aborted = e instanceof DOMException && e.name === "AbortError";
      setMessages((m) => m.map((msg, i) => (i === idx ? { ...msg, content: msg.content || (aborted ? "⏹ Stopped." : String(e)), running: false } : msg)));
    } finally {
      setBusy(false);
      abortRef.current = null;
    }
  };

  // fire the seed question once on mount (e.g. clicked "Analyze this model")
  const seededRef = useRef(false);
  useEffect(() => {
    if (seedQuestion && !seededRef.current) { seededRef.current = true; void send(seedQuestion); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seedQuestion]);

  return (
    <div className="flex h-full min-h-0">
      {/* chat column */}
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b border-border px-4 py-2.5">
          <Sparkles className="h-4 w-4 text-[#2DD4BF]" />
          <span className="text-sm font-semibold">{T.title}</span>
          {scopeTables.length > 0 && (
            <span className="inline-flex items-center gap-1 rounded-full bg-[#2DD4BF]/10 px-2 py-0.5 text-[11px] font-medium text-[#2DD4BF]">
              <Table2 className="h-3 w-3" /> {scopeTables.join(", ")}
            </span>
          )}
          <button onClick={onClose} className="ml-auto rounded p-1 text-muted-foreground hover:bg-secondary hover:text-foreground"><X className="h-4 w-4" /></button>
        </div>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
          {messages.length === 0 && (
            <div className="flex h-full flex-col items-center justify-center gap-2 text-center text-muted-foreground">
              <BotAvatar className="h-10 w-10" />
              <div className="text-sm">{T.etlAskPlaceholder}</div>
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`flex gap-3 ${m.role === "user" ? "justify-end" : ""}`}>
              {m.role === "assistant" && <BotAvatar className="h-7 w-7 shrink-0" />}
              <div className={`min-w-0 ${m.role === "user" ? "max-w-[85%]" : "flex-1"}`}>
                {m.role === "assistant" && m.trace && m.trace.length > 0 && <TracePanel trace={m.trace} running={m.running} sourceMode="database" />}
                <div className={`rounded-2xl px-3.5 py-2 text-sm ${m.role === "user" ? "bg-[#2DD4BF] text-white" : "bg-secondary/60"}`}>
                  {m.role === "assistant" ? (
                    m.content ? <Markdown content={m.content} /> : m.running ? <span className="inline-flex items-center gap-1.5 text-muted-foreground"><Loader2 className="h-3.5 w-3.5 animate-spin" /> …</span> : null
                  ) : m.content}
                </div>
              </div>
              {m.role === "user" && <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-secondary"><User className="h-4 w-4" /></div>}
            </div>
          ))}
          <div ref={endRef} />
        </div>

        <div className="border-t border-border p-3">
          <div className="flex items-end gap-2">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
              rows={1}
              placeholder={T.etlAskPlaceholder}
              className="max-h-32 min-h-[38px] flex-1 resize-none rounded-lg border border-border bg-background px-3 py-2 text-sm"
            />
            {busy ? (
              <button onClick={() => abortRef.current?.abort()} className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-border px-3 text-sm hover:bg-secondary"><Square className="h-3.5 w-3.5" /></button>
            ) : (
              <button onClick={() => send()} disabled={!input.trim()} className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-[#2DD4BF] px-4 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"><Send className="h-4 w-4" /></button>
            )}
          </div>
        </div>
      </div>

      {/* artifact preview (only when present) */}
      {artifact && (
        <aside className="flex w-[46%] min-w-[320px] shrink-0 flex-col border-l border-border bg-card">
          <div className="flex items-center gap-2 border-b border-border px-4 py-2.5">
            {artifactIcon(artifact.kind)}
            <span className="min-w-0 flex-1 truncate text-sm font-semibold">{artifact.title}</span>
            <span className="rounded bg-secondary px-1.5 py-0.5 text-[10px] uppercase text-muted-foreground">{artifact.kind}</span>
            <button onClick={() => api.downloadWorkspaceFile(artifact.file_id, artifact.workspace_file)} className="rounded p-1 text-muted-foreground hover:bg-secondary hover:text-foreground"><Download className="h-4 w-4" /></button>
            <button onClick={() => setActiveArtifactId(null)} className="rounded p-1 text-muted-foreground hover:bg-secondary hover:text-foreground"><X className="h-4 w-4" /></button>
          </div>
          {artifacts.length > 1 && (
            <div className="flex flex-wrap gap-1 border-b border-border px-3 py-2">
              {artifacts.map((a) => (
                <button key={a.file_id} onClick={() => setActiveArtifactId(a.file_id)} className={`inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] ${a.file_id === artifact.file_id ? "bg-secondary" : "hover:bg-secondary/60"}`}>
                  {artifactIcon(a.kind)}<span className="max-w-[120px] truncate">{a.title}</span>
                </button>
              ))}
            </div>
          )}
          <div className="min-h-0 flex-1">
            {artifactHtml ? (
              <iframe title={artifact.title ?? "artifact"} srcDoc={artifactHtml} sandbox="allow-same-origin allow-scripts" className="h-full w-full border-0 bg-white" />
            ) : (
              <div className="flex h-full items-center justify-center text-sm text-muted-foreground"><Loader2 className="mr-2 h-4 w-4 animate-spin" /> {T.renderingPreview}</div>
            )}
          </div>
        </aside>
      )}
    </div>
  );
}
