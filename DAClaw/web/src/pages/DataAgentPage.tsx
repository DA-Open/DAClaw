import { useEffect, useRef, useState } from "react";
import {
  Database,
  Send,
  Table2,
  Download,
  Upload,
  Paperclip,
  Loader2,
  X,
  User,
  BarChart3,
  FileText,
  FileSpreadsheet,
  ChevronDown,
  ChevronRight,
  Plus,
  MessageSquare,
  Trash2,
  Pencil,
  Copy,
  Check,
  Square,
  Files,
  Plug,
  BookOpen,
  BrainCircuit,
  FileSearch,
  Network,
  Orbit,
  Sparkles,
  ShieldCheck,
  Zap,
} from "lucide-react";
import { api } from "@/lib/api";
import type { DATableInfo, ChatRunEvent, ChatTraceEvent, DAArtifact, DAKnowledgeSource, DAWorkflow, DAWorkflowSummary, SkillInfo, SkillContent } from "@/lib/api";
import { Markdown } from "@/components/Markdown";
import { TracePanel, appendTraceEvent, sanitizeTrace, type SourceMode } from "@/components/AgentTrace";
import { BotAvatar } from "@/components/BotAvatar";
import { ConnectDbDialog } from "@/components/ConnectDbDialog";
import { WorkflowMission } from "@/components/WorkflowMission";
import { useI18n } from "@/i18n";
import type { Translations } from "@/i18n";

type DaT = Translations["da"];

type ClarifyPrompt = { runId: string; question: string; choices: string[] };
type ChatMsg = {
  role: "user" | "assistant";
  content: string;
  trace?: ChatTraceEvent[];
  running?: boolean;
  workflow?: DAWorkflow | null;
  clarify?: ClarifyPrompt | null;
};

type Conversation = {
  id: string;
  title: string;
  messages: ChatMsg[];
  artifacts: DAArtifact[];
  activeArtifactId: string | null;
  sourceMode: SourceMode;
  updatedAt: number;
};

const DB_KINDS = new Set(["sqlite", "duckdb", "db"]);
const PERSIST_KEY = "daclaw.dataAgent.v2";
const MAX_CONVOS = 30;
const MSG_TEXT_LIMIT = 120_000;

function newId(): string {
  // no Date.now/Math.random restriction in the browser
  return `c_${Date.now().toString(36)}_${Math.floor(Math.random() * 1e6).toString(36)}`;
}

function newConversation(): Conversation {
  return {
    id: newId(),
    title: "New chat",
    messages: [],
    artifacts: [],
    activeArtifactId: null,
    sourceMode: "database",
    updatedAt: Date.now(),
  };
}

function loadConversations(): { conversations: Conversation[]; activeId: string } {
  try {
    const raw = localStorage.getItem(PERSIST_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as { conversations: Conversation[]; activeId: string };
      if (parsed.conversations?.length) {
        return { conversations: parsed.conversations, activeId: parsed.activeId || parsed.conversations[0].id };
      }
    }
  } catch {
    /* ignore */
  }
  const c = newConversation();
  return { conversations: [c], activeId: c.id };
}

function titleFrom(text: string): string {
  const t = text.trim().replace(/\s+/g, " ");
  return t.length > 40 ? t.slice(0, 40) + "…" : t || "New chat";
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isTerminal(status: string | undefined): boolean {
  return status === "completed" || status === "failed" || status === "cancelled";
}

function fmtCount(n: number | null | undefined): string {
  if (n == null) return "";
  return n.toLocaleString();
}

function artifactIcon(kind: string) {
  if (kind === "document") return <FileText className="h-3.5 w-3.5 text-[#2DD4BF]" />;
  if (kind === "excel") return <FileSpreadsheet className="h-3.5 w-3.5 text-emerald-600" />;
  if (kind === "story") return <Orbit className="h-3.5 w-3.5 text-violet-300" />;
  return <BarChart3 className="h-3.5 w-3.5 text-[#2DD4BF]" />;
}

/** Clarify question card — shown in-chat when the agent needs the user to
 *  disambiguate. Answering resumes the parked run on the server. */
function ClarifyCard({ prompt, onAnswer, T }: { prompt: ClarifyPrompt; onAnswer: (answer: string) => void; T: DaT }) {
  const [other, setOther] = useState("");
  return (
    <div className="mt-2 overflow-hidden rounded-2xl border border-cyan-300/30 bg-cyan-300/5">
      <div className="flex items-center gap-2 border-b border-cyan-300/15 px-4 py-2.5">
        <BrainCircuit className="h-4 w-4 text-cyan-300" />
        <span className="text-xs font-bold text-cyan-200">{T.clarifyTitle}</span>
      </div>
      <div className="space-y-2.5 p-4">
        <div className="text-sm text-slate-200">{prompt.question}</div>
        {prompt.choices.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {prompt.choices.map((choice) => (
              <button
                key={choice}
                onClick={() => onAnswer(choice)}
                className="rounded-lg border border-cyan-300/25 bg-cyan-300/5 px-3 py-1.5 text-xs font-medium text-slate-100 hover:border-cyan-300/50 hover:bg-cyan-300/10"
              >
                {choice}
              </button>
            ))}
          </div>
        )}
        <div className="flex items-center gap-2">
          <input
            value={other}
            onChange={(e) => setOther(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && other.trim()) onAnswer(other); }}
            placeholder={T.clarifyOther}
            className="min-w-0 flex-1 rounded-lg border border-white/10 bg-white/[0.03] px-3 py-1.5 text-xs text-slate-200 placeholder:text-slate-600"
          />
          <button
            onClick={() => other.trim() && onAnswer(other)}
            disabled={!other.trim()}
            className="rounded-lg bg-cyan-400 px-3 py-1.5 text-xs font-semibold text-slate-900 hover:opacity-90 disabled:opacity-40"
          >
            {T.clarifySend}
          </button>
        </div>
      </div>
    </div>
  );
}

/** Compact sample-rows table for the left DB browser. */
function SampleTable({ columns, rows }: { columns: string[]; rows: Record<string, unknown>[] }) {
  return (
    <div className="overflow-x-auto rounded-md border border-border">
      <table className="w-full text-[11px]">
        <thead>
          <tr className="border-b border-border bg-secondary/50">
            {columns.map((c) => (
              <th key={c} className="px-2 py-1 text-left font-semibold">{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 10).map((row, i) => (
            <tr key={i} className="border-b border-border last:border-0">
              {columns.map((c) => (
                <td key={c} className="px-2 py-1 tabular-nums">
                  {row[c] == null ? <span className="text-muted-foreground">null</span> : String(row[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function DataAgentPage() {
  const { t } = useI18n();
  const T = t.da;
  const initial = loadConversations();
  const [conversations, setConversations] = useState<Conversation[]>(initial.conversations);
  const [activeId, setActiveId] = useState<string>(initial.activeId);

  const active = conversations.find((c) => c.id === activeId) ?? conversations[0];

  const [tables, setTables] = useState<DATableInfo[]>([]);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [source, setSource] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [connectMsg, setConnectMsg] = useState<string | null>(null);

  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [copiedIdx, setCopiedIdx] = useState<number | null>(null);
  const [convosOpen, setConvosOpen] = useState(true);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameText, setRenameText] = useState("");
  const [dbDialogOpen, setDbDialogOpen] = useState(false);
  const [knowledge, setKnowledge] = useState<DAKnowledgeSource[]>([]);
  const [knowledgeOpen, setKnowledgeOpen] = useState(true);
  const [knowledgeBusy, setKnowledgeBusy] = useState(false);
  const [playbooks, setPlaybooks] = useState<DAWorkflowSummary[]>([]);
  const [playbooksOpen, setPlaybooksOpen] = useState(true);
  // skills — the full library, shown in the left panel (moved out of the top-nav
  // dropdown). Agent-generated skills that pass quality validation appear here.
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [skillsOpen, setSkillsOpen] = useState(true);
  const [skillSearch, setSkillSearch] = useState("");
  const [freshSkill, setFreshSkill] = useState<string | null>(null); // just-generated skill name (highlight)
  const [viewSkill, setViewSkill] = useState<SkillContent | null>(null); // open skill viewer modal
  const [viewSkillLoading, setViewSkillLoading] = useState<string | null>(null);
  const [draftWorkflow, setDraftWorkflow] = useState<DAWorkflow | null>(null);
  const [draftText, setDraftText] = useState("");
  const [compiling, setCompiling] = useState(false);

  // left browser: which table is expanded + its sample
  const [openTable, setOpenTable] = useState<string | null>(null);
  const [sample, setSample] = useState<{ columns: string[]; rows: Record<string, unknown>[] } | null>(null);
  const [sampleLoading, setSampleLoading] = useState(false);
  const [selectedTables, setSelectedTables] = useState<string[]>([]);
  // which tables have their column chips expanded (collapsed by default to cut scrolling)
  const [colsOpen, setColsOpen] = useState<Set<string>>(new Set());
  const toggleCols = (table: string) =>
    setColsOpen((s) => { const n = new Set(s); n.has(table) ? n.delete(table) : n.add(table); return n; });

  // right artifact panel (preview state, derived from active conversation's activeArtifact)
  const [artifactHtml, setArtifactHtml] = useState<string | null>(null);
  const [artifactSheets, setArtifactSheets] = useState<
    { name: string; columns: { name: string }[]; row_count: number | null; sample: Record<string, unknown>[] }[] | null
  >(null);
  const [artifactOpen, setArtifactOpen] = useState(true);
  // Default the artifact panel to ~44% of the space right of the sidebar so the
  // chat column keeps a slight majority (it felt cramped at a full 50/50 split).
  // Still fully drag-resizable via the handle below.
  const [artifactWidth, setArtifactWidth] = useState(() =>
    Math.min(860, Math.max(360, Math.round((window.innerWidth - 300) * 0.44)))
  );
  const [artifactResizing, setArtifactResizing] = useState(false);
  const [activeSheet, setActiveSheet] = useState(0);
  const [showArtifactList, setShowArtifactList] = useState(false);

  const [sidebarWidth, setSidebarWidth] = useState(300);
  const [sidebarResizing, setSidebarResizing] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const knowledgeInputRef = useRef<HTMLInputElement>(null);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const deltaBufferRef = useRef("");
  const deltaTimerRef = useRef<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const artifact = active?.artifacts.find((a) => a.file_id === active.activeArtifactId) ?? null;
  const sourceMode = active?.sourceMode ?? "database";
  const messages = active?.messages ?? [];

  // ---- conversation mutation helpers --------------------------------------
  // Always target an explicit conversation id so async streaming updates never
  // land on the wrong conversation after the user switches/creates chats.
  const updateConvo = (id: string, fn: (c: Conversation) => Conversation) => {
    setConversations((cs) => cs.map((c) => (c.id === id ? { ...fn(c), updatedAt: Date.now() } : c)));
  };
  const updateActive = (fn: (c: Conversation) => Conversation) => updateConvo(activeId, fn);
  const setMessages = (fn: (m: ChatMsg[]) => ChatMsg[]) => {
    updateActive((c) => ({ ...c, messages: fn(c.messages) }));
  };
  const setConvoMessages = (id: string, fn: (m: ChatMsg[]) => ChatMsg[]) => {
    updateConvo(id, (c) => ({ ...c, messages: fn(c.messages) }));
  };

  const refreshTables = () => api.daTables().then((r) => setTables(r.tables)).catch(() => {});
  // Recommend starter questions once tables are connected (inferred from the
  // schema). Only fetch when there are tables but no messages yet — they're an
  // onboarding aid for a fresh conversation, not a persistent element.
  useEffect(() => {
    if (tables.length === 0 || messages.length > 0) { setSuggestions([]); return; }
    let cancelled = false;
    api.daSuggest(4).then((r) => { if (!cancelled) setSuggestions(r.suggestions || []); }).catch(() => {});
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tables.length, messages.length]);
  const refreshKnowledge = () => api.daKnowledge().then((r) => setKnowledge(r.sources)).catch(() => {});
  const refreshPlaybooks = () => api.daWorkflows().then((r) => setPlaybooks(r.workflows)).catch(() => {});
  const refreshSkills = () => api.getSkills().then((r) => setSkills(r)).catch(() => {});
  const openSkill = async (name: string) => {
    setViewSkillLoading(name);
    try {
      const c = await api.getSkillContent(name);
      setViewSkill(c);
    } catch (e) {
      setConnectMsg(String(e));
    } finally {
      setViewSkillLoading(null);
    }
  };
  useEffect(() => {
    refreshTables();
    refreshKnowledge();
    refreshPlaybooks();
    refreshSkills();
  }, []);
  // Handoff from the Data Engineering page: preselect a model to analyze.
  useEffect(() => {
    try {
      const raw = localStorage.getItem("daclaw.dataAgent.preselect");
      if (raw) {
        const p = JSON.parse(raw) as { tables?: string[] };
        if (p.tables?.length) setSelectedTables(p.tables);
        localStorage.removeItem("daclaw.dataAgent.preselect");
      }
    } catch { /* ignore */ }
  }, []);
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // persist to localStorage (truncate large content so quota isn't exceeded)
  useEffect(() => {
    try {
      const trimmed = conversations.slice(0, MAX_CONVOS).map((c) => ({
        ...c,
        messages: c.messages.map((m) => ({
          ...m,
          running: false,
          content: m.content.length > MSG_TEXT_LIMIT ? m.content.slice(0, MSG_TEXT_LIMIT) + "\n…[truncated]" : m.content,
        })),
      }));
      localStorage.setItem(PERSIST_KEY, JSON.stringify({ conversations: trimmed, activeId }));
    } catch {
      /* quota — non-fatal */
    }
  }, [conversations, activeId]);

  // ---- artifact panel resize ----------------------------------------------
  useEffect(() => {
    if (!artifactResizing) return;
    const onMove = (e: MouseEvent) => setArtifactWidth(Math.min(1100, Math.max(360, window.innerWidth - e.clientX)));
    const onUp = () => setArtifactResizing(false);
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
  }, [artifactResizing]);

  useEffect(() => {
    if (!sidebarResizing) return;
    const onMove = (e: MouseEvent) => setSidebarWidth(Math.min(560, Math.max(240, e.clientX)));
    const onUp = () => setSidebarResizing(false);
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
  }, [sidebarResizing]);

  // fetch preview when the active artifact changes
  useEffect(() => {
    if (!artifact) return;
    setArtifactHtml(null);
    setArtifactSheets(null);
    setActiveSheet(0);
    let cancelled = false;
    (async () => {
      try {
        const data = await api.getWorkspaceFilePreview(artifact.file_id);
        const p = data.preview;
        if (cancelled || !p) return;
        if (p.type === "html") setArtifactHtml(p.html);
        else if (p.type === "database")
          setArtifactSheets(p.tables.map((t) => ({ name: t.name, columns: t.columns, row_count: t.row_count, sample: t.sample })));
      } catch {
        /* download card only */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [artifact?.file_id]);

  // ---- connect -------------------------------------------------------------
  const applySourceKind = (kind?: string) => {
    if (kind) updateActive((c) => ({ ...c, sourceMode: DB_KINDS.has(kind) ? "database" : "table" }));
  };

  const connect = async () => {
    if (!source.trim()) return;
    setConnecting(true);
    setConnectMsg(null);
    try {
      const r = await api.daConnect(source.trim());
      if (r.ok) {
        applySourceKind(r.kind);
        setConnectMsg(`Connected ${r.connected} (${r.kind})`);
        refreshTables();
      } else setConnectMsg(`Error: ${r.error}`);
    } catch (e) {
      setConnectMsg(String(e));
    } finally {
      setConnecting(false);
    }
  };

  const uploadAndConnect = async (files: FileList | File[]) => {
    const list = Array.from(files);
    if (!list.length) return;
    setConnecting(true);
    setConnectMsg(`Uploading ${list.length} file(s)…`);
    try {
      for (const file of list) {
        const up = await api.uploadChatFile(file);
        setMessages((m) => [...m, { role: "assistant", content: `📎 Uploaded ${up.name} — connecting…` }]);
        const r = await api.daConnect(up.path);
        if (r.ok) {
          applySourceKind(r.kind);
          setMessages((m) => [...m, { role: "assistant", content: `Connected ${up.name}: ${r.tables?.map((t) => t.table).join(", ")}` }]);
        } else {
          setMessages((m) => [...m, { role: "assistant", content: `Could not read ${up.name}: ${r.error}` }]);
        }
      }
      refreshTables();
    } catch (e) {
      setConnectMsg(String(e));
    } finally {
      setConnecting(false);
      setConnectMsg(null);
    }
  };

  const uploadKnowledge = async (files: FileList | File[]) => {
    const list = Array.from(files);
    if (!list.length) return;
    setKnowledgeBusy(true);
    try {
      for (const file of list) {
        const up = await api.uploadChatFile(file);
        const result = await api.daKnowledgeConnect(up.path, up.name);
        if (!result.ok) throw new Error(result.error || `Could not index ${up.name}`);
      }
      refreshKnowledge();
    } catch (e) {
      setConnectMsg(String(e));
    } finally {
      setKnowledgeBusy(false);
    }
  };

  // ---- window drag-drop ----------------------------------------------------
  useEffect(() => {
    let depth = 0;
    const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types ?? []).includes("Files");
    const onDragEnter = (e: DragEvent) => { if (!hasFiles(e)) return; e.preventDefault(); depth += 1; setDragOver(true); };
    const onDragOver = (e: DragEvent) => { if (!hasFiles(e)) return; e.preventDefault(); if (e.dataTransfer) e.dataTransfer.dropEffect = "copy"; };
    const onDragLeave = (e: DragEvent) => { if (!hasFiles(e)) return; depth = Math.max(0, depth - 1); if (depth === 0) setDragOver(false); };
    const onDrop = (e: DragEvent) => { e.preventDefault(); depth = 0; setDragOver(false); const f = e.dataTransfer?.files; if (f?.length) uploadAndConnect(f); };
    window.addEventListener("dragenter", onDragEnter);
    window.addEventListener("dragover", onDragOver);
    window.addEventListener("dragleave", onDragLeave);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragenter", onDragEnter);
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("dragleave", onDragLeave);
      window.removeEventListener("drop", onDrop);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  const toggleTable = async (table: string) => {
    if (openTable === table) { setOpenTable(null); setSample(null); return; }
    setOpenTable(table);
    setSample(null);
    setSampleLoading(true);
    try {
      const r = await api.daTableSample(table, 10);
      if (r.ok && r.columns && r.sample_rows) setSample({ columns: r.columns, rows: r.sample_rows });
    } catch {
      /* ignore */
    } finally {
      setSampleLoading(false);
    }
  };

  const toggleSelect = (table: string) => {
    setSelectedTables((s) => (s.includes(table) ? s.filter((t) => t !== table) : [...s, table]));
  };
  // drop selections for tables that no longer exist (after reconnect/reset).
  // Skip while tables is still empty (initial load) so a cross-page preselect
  // isn't wiped before the table list resolves.
  useEffect(() => {
    if (tables.length === 0) return;
    setSelectedTables((s) => s.filter((t) => tables.some((tb) => tb.table === t)));
  }, [tables]);

  // ---- streaming agent chat -----------------------------------------------
  // All updates target an explicit convoId so switching chats mid-stream never
  // corrupts or loses another conversation's messages.
  const flushDelta = (convoId: string, idx: number) => {
    const d = deltaBufferRef.current;
    if (!d) return;
    deltaBufferRef.current = "";
    setConvoMessages(convoId, (m) => m.map((msg, i) => (i === idx ? { ...msg, content: msg.content + d } : msg)));
  };
  const scheduleDelta = (convoId: string, idx: number, d: string) => {
    deltaBufferRef.current += d;
    if (deltaTimerRef.current !== null) return;
    deltaTimerRef.current = window.setTimeout(() => { deltaTimerRef.current = null; flushDelta(convoId, idx); }, 120);
  };

  const addArtifact = (convoId: string, art: DAArtifact) => {
    updateConvo(convoId, (c) => {
      const exists = c.artifacts.some((a) => a.file_id === art.file_id);
      return {
        ...c,
        artifacts: exists ? c.artifacts.map((a) => (a.file_id === art.file_id ? art : a)) : [...c.artifacts, art],
        activeArtifactId: art.file_id,
      };
    });
    // only pop the panel open if this stream belongs to the visible conversation
    if (convoId === activeId) { setArtifactOpen(true); setShowArtifactList(false); }
  };

  const onSkillEvent = (skill: NonNullable<ChatTraceEvent["skill"]>) => {
    // A skill_manage create/edit completed. If it was kept (passed quality
    // validation), refresh the left library and briefly highlight the new one.
    if (skill.kept) {
      refreshSkills();
      setFreshSkill(skill.name);
      setTimeout(() => setFreshSkill((cur) => (cur === skill.name ? null : cur)), 30_000);
    }
  };

  // Answer a parked clarify() question; the run resumes on the server.
  const answerClarify = (convoId: string, idx: number, prompt: ClarifyPrompt, answer: string) => {
    const text = answer.trim();
    if (!text || !prompt.runId) return;
    setConvoMessages(convoId, (m) => m.map((msg, i) => (i === idx ? { ...msg, clarify: null } : msg)));
    api.daClarify(prompt.runId, text).catch(() => {});
  };

  const applyEvent = (convoId: string, idx: number, event: ChatRunEvent, runId?: string) => {
    if (event.type === "assistant_delta" && "delta" in event) { scheduleDelta(convoId, idx, event.delta); return; }
    if (event.type === "heartbeat") return;
    if (event.type === "clarify") {
      // Agent asked a clarifying question; park a question card on the message.
      const ev = event as unknown as { question?: string; choices?: string[] };
      setConvoMessages(convoId, (m) => m.map((msg, i) => (i === idx ? { ...msg, clarify: { runId: runId || "", question: ev.question || "", choices: ev.choices || [] } } : msg)));
      return;
    }
    if (event.type === "tool_complete" && (event as ChatTraceEvent).artifact) addArtifact(convoId, (event as ChatTraceEvent).artifact!);
    if (event.type === "tool_complete" && (event as ChatTraceEvent).skill) onSkillEvent((event as ChatTraceEvent).skill!);
    if (event.type === "done" && "final_response" in event) {
      if (deltaTimerRef.current !== null) { window.clearTimeout(deltaTimerRef.current); deltaTimerRef.current = null; }
      flushDelta(convoId, idx);
      (event.trace || []).filter((t) => t.artifact).forEach((t) => addArtifact(convoId, t.artifact!));
      (event.trace || []).filter((t) => t.skill).forEach((t) => onSkillEvent(t.skill!));
      setConvoMessages(convoId, (m) => m.map((msg, i) => (i === idx ? { ...msg, content: event.final_response || msg.content, trace: sanitizeTrace(event.trace), running: false, clarify: null } : msg)));
      return;
    }
    if (event.type === "error") {
      setConvoMessages(convoId, (m) => m.map((msg, i) => (i === idx ? { ...msg, content: msg.content || `Request failed: ${event.message}`, running: false, trace: appendTraceEvent(msg.trace, event) } : msg)));
      return;
    }
    setConvoMessages(convoId, (m) => m.map((msg, i) => (i === idx ? { ...msg, trace: appendTraceEvent(msg.trace, event) } : msg)));
  };

  const compileDraft = async () => {
    const text = input.trim();
    if (!text || busy || compiling) return;
    setCompiling(true);
    try {
      const workflow = await api.daWorkflowPlan(
        text,
        selectedTables.length > 0 ? selectedTables : tables.map((table) => table.table),
      );
      setDraftWorkflow(workflow);
      setDraftText(text);
      refreshPlaybooks();
    } catch (e) {
      setConnectMsg(String(e));
    } finally {
      setCompiling(false);
    }
  };

  const removeDraftNode = async (nodeId: string) => {
    if (!draftWorkflow) return;
    const removed = draftWorkflow.nodes.find((node) => node.id === nodeId);
    if (!removed) return;
    const bypass = removed.depends_on || [];
    const nodes = draftWorkflow.nodes
      .filter((node) => node.id !== nodeId)
      .map((node) => ({
        ...node,
        depends_on: Array.from(new Set(node.depends_on.flatMap((dep) => dep === nodeId ? bypass : [dep]))),
      }));
    try {
      const compiled = await api.daWorkflowCompile({ ...draftWorkflow, nodes });
      setDraftWorkflow(compiled);
    } catch (e) {
      setConnectMsg(String(e));
    }
  };

  const send = async (explicit?: string, preparedWorkflow?: DAWorkflow | null) => {
    const text = (explicit ?? input).trim();
    if (!text || busy) return;
    setInput("");
    setBusy(true);
    const convoId = activeId;
    const history = messages.map((m) => ({ role: m.role, content: m.content }));

    // auto-title from first user message
    if (messages.length === 0) updateConvo(convoId, (c) => ({ ...c, title: titleFrom(text) }));

    const workflow = preparedWorkflow ?? await api
      .daWorkflowPlan(text, selectedTables.length > 0 ? selectedTables : tables.map((table) => table.table))
      .catch(() => null);
    setDraftWorkflow(null);
    setDraftText("");
    let idx = 0;
    setConvoMessages(convoId, (m) => { idx = m.length + 1; return [...m, { role: "user", content: text }, { role: "assistant", content: "", running: true, workflow }]; });

    // If the user has selected specific tables, tell the agent to scope to them.
    // The scope hint is sent to the LLM but NOT shown in the chat bubble.
    const scoped = selectedTables.length > 0
      ? `${text}\n\n[Scope: focus this request on the following table(s) in the connected ${sourceMode === "table" ? "workbook" : "database"}: ${selectedTables.join(", ")}. Query/analyze these unless I say otherwise.]`
      : text;

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const run = await api.startDaAgentRun({ message: scoped, history, workflow });
      let after = -1;
      while (true) {
        if (ctrl.signal.aborted) throw new DOMException("Aborted", "AbortError");
        const payload = await api.getChatRunEvents(run.run_id, after);
        for (const event of payload.events) { after = event.seq ?? after; applyEvent(convoId, idx, event, run.run_id); }
        if (isTerminal(payload.run.status)) break;
        await delay(payload.events.length > 0 ? 200 : 900);
      }
    } catch (e) {
      const aborted = e instanceof DOMException && e.name === "AbortError";
      setConvoMessages(convoId, (m) => m.map((msg, i) => (i === idx ? { ...msg, content: msg.content || (aborted ? "⏹ Stopped." : String(e)), running: false } : msg)));
    } finally {
      setBusy(false);
      abortRef.current = null;
    }
  };

  const stop = () => { abortRef.current?.abort(); };

  const askDashboard = (table: string) => { if (!busy) void send(`Give me an overview dashboard of the "${table}" table.`); };

  const copyMessage = (idx: number, text: string) => {
    navigator.clipboard?.writeText(text).then(() => { setCopiedIdx(idx); setTimeout(() => setCopiedIdx(null), 1200); }, () => {});
  };

  // ---- conversation management --------------------------------------------
  const createConversation = () => {
    if (busy) return;
    const c = newConversation();
    setConversations((cs) => [c, ...cs].slice(0, MAX_CONVOS));
    setActiveId(c.id);
  };
  const deleteConversation = (id: string) => {
    setConversations((cs) => {
      const rest = cs.filter((c) => c.id !== id);
      if (rest.length === 0) { const c = newConversation(); setActiveId(c.id); return [c]; }
      if (id === activeId) setActiveId(rest[0].id);
      return rest;
    });
  };
  const startRename = (c: Conversation) => { setRenamingId(c.id); setRenameText(c.title); };
  const commitRename = () => {
    const id = renamingId;
    if (!id) return;
    const title = renameText.trim();
    if (title) updateConvo(id, (c) => ({ ...c, title }));
    setRenamingId(null);
    setRenameText("");
  };
  const cancelRename = () => { setRenamingId(null); setRenameText(""); };
  const removeArtifact = async (art: DAArtifact) => {
    try { await api.deleteWorkspaceFile(art.file_id); } catch { /* ignore */ }
    updateActive((c) => {
      const artifacts = c.artifacts.filter((a) => a.file_id !== art.file_id);
      return { ...c, artifacts, activeArtifactId: c.activeArtifactId === art.file_id ? (artifacts[artifacts.length - 1]?.file_id ?? null) : c.activeArtifactId };
    });
  };

  return (
    <div className="mission-shell relative flex h-full overflow-hidden bg-[#1a2536] text-slate-100">
      <ConnectDbDialog
        open={dbDialogOpen}
        onClose={() => setDbDialogOpen(false)}
        onConnected={() => { updateActive((c) => ({ ...c, sourceMode: "database" })); refreshTables(); }}
      />
      {dragOver && (
        <div className="pointer-events-none fixed inset-0 z-50 flex flex-col items-center justify-center bg-background/70 backdrop-blur-sm">
          <div className="flex flex-col items-center rounded-3xl border-2 border-dashed border-[#2DD4BF] bg-[#2DD4BF]/10 px-16 py-12">
            <Upload className="mb-3 h-12 w-12 text-[#2DD4BF]" />
            <div className="text-xl font-semibold text-[#2DD4BF]">Drop to analyze</div>
            <div className="mt-1 text-sm text-muted-foreground">CSV · Excel · Parquet · SQLite · DuckDB</div>
          </div>
        </div>
      )}

      {/* Left: conversations + data sources */}
      {mobileSidebarOpen && <button type="button" aria-label="Close data panel" onClick={() => setMobileSidebarOpen(false)} className="data-sidebar-backdrop fixed inset-0 z-50 hidden bg-black/55 backdrop-blur-sm" />}
      <aside style={{ width: sidebarWidth }} className={`data-sidebar mission-sidebar flex shrink-0 flex-col border-r border-cyan-300/10 bg-[#212e42]/95 ${mobileSidebarOpen ? "data-sidebar-open" : ""}`}>
        {/* conversations */}
        <div className="border-b border-border">
          <div className="flex items-center gap-2 px-4 pt-4 pb-2">
            <button onClick={() => setConvosOpen((v) => !v)} className="flex items-center gap-1.5 text-sm font-semibold">
              {convosOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
              <MessageSquare className="h-4 w-4 text-[#2DD4BF]" /> {T.conversations}
            </button>
            <button onClick={createConversation} disabled={busy} title="New chat" className="ml-auto inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] hover:bg-secondary disabled:opacity-50">
              <Plus className="h-3.5 w-3.5" /> {T.newChat}
            </button>
            <button onClick={() => setMobileSidebarOpen(false)} className="data-sidebar-close hidden rounded-lg border border-white/7 p-1.5 text-slate-500 hover:text-slate-100"><X className="h-3.5 w-3.5" /></button>
          </div>
          {convosOpen && (
            <div className="max-h-56 overflow-y-auto px-2 pb-2">
              {conversations.map((c) => (
                <div key={c.id} className={`group flex items-center gap-1.5 rounded-md px-2 py-1.5 text-xs ${c.id === activeId ? "bg-secondary" : "hover:bg-secondary/60"}`}>
                  {renamingId === c.id ? (
                    <input
                      autoFocus
                      value={renameText}
                      onChange={(e) => setRenameText(e.target.value)}
                      onBlur={commitRename}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") { e.preventDefault(); commitRename(); }
                        else if (e.key === "Escape") { e.preventDefault(); cancelRename(); }
                      }}
                      className="min-w-0 flex-1 rounded border border-[#2DD4BF] bg-background px-1.5 py-0.5 text-xs outline-none"
                    />
                  ) : (
                    <>
                      <button
                        onClick={() => setActiveId(c.id)}
                        onDoubleClick={() => startRename(c)}
                        className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
                      >
                        <MessageSquare className="h-3 w-3 shrink-0 text-muted-foreground" />
                        <span className="min-w-0 flex-1 truncate">{c.title}</span>
                        {c.artifacts.length > 0 && <span className="text-[9px] text-muted-foreground">{c.artifacts.length}📄</span>}
                      </button>
                      <button onClick={() => startRename(c)} title={T.rename} className="opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-[#2DD4BF]">
                        <Pencil className="h-3 w-3" />
                      </button>
                      <button onClick={() => deleteConversation(c.id)} title={T.deleteChat} className="opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-red-500">
                        <Trash2 className="h-3 w-3" />
                      </button>
                    </>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        {/* data sources */}
        <div className="border-b border-white/5 p-4">
          <div className="mb-2 flex items-center gap-2 text-sm font-semibold">
            <Database className="h-4 w-4 text-cyan-300" /> {T.dataSources}
            <span className="ml-auto rounded-full bg-cyan-300/10 px-2 py-0.5 text-[9px] font-black tabular-nums text-cyan-200">{tables.length}</span>
          </div>
          <button
            onClick={() => setDbDialogOpen(true)}
            className="mission-primary mb-2 flex w-full items-center justify-center gap-1.5 rounded-lg px-3 py-2 text-xs font-bold text-[#041817]"
          >
            <Plug className="h-3.5 w-3.5" /> {T.connectDb}
          </button>
          <div className="flex gap-1.5">
            <input value={source} onChange={(e) => setSource(e.target.value)} onKeyDown={(e) => e.key === "Enter" && connect()}
              placeholder={T.sourcePlaceholder} className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1.5 text-xs" />
            <button onClick={connect} disabled={connecting} className="rounded-md border border-border px-3 py-1.5 text-xs font-medium hover:bg-secondary disabled:opacity-50">{T.add}</button>
          </div>
          {connectMsg && <div className="mt-2 text-[11px] text-muted-foreground">{connectMsg}</div>}
        </div>
        <div className="flex-1 overflow-y-auto p-3">
          {tables.length === 0 ? (
            <div className="px-1 py-4 text-xs text-muted-foreground">{T.dragToConnectHint}</div>
          ) : (
            <div className="space-y-2">
              {tables.map((t) => {
                const isSel = selectedTables.includes(t.table);
                return (
                <div
                  key={t.table}
                  onClick={() => toggleSelect(t.table)}
                  title={T.selectForChat}
                  className={`cursor-pointer rounded-lg border bg-card transition-colors ${isSel ? "border-[#2DD4BF] ring-1 ring-[#2DD4BF]/40" : "border-border hover:border-[#2DD4BF]/50"}`}
                >
                  <div className="flex w-full items-center gap-1.5 px-2.5 py-2">
                    <button
                      onClick={(e) => { e.stopPropagation(); toggleTable(t.table); }}
                      title={openTable === t.table ? "Collapse" : "Preview rows"}
                      className="rounded p-0.5 text-muted-foreground hover:bg-secondary hover:text-foreground"
                    >
                      {openTable === t.table ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                    </button>
                    <Table2 className={`h-3.5 w-3.5 ${isSel ? "text-[#2DD4BF]" : "text-muted-foreground"}`} />
                    <span className="min-w-0 flex-1 truncate font-mono text-xs font-semibold">{t.table}</span>
                    {t.row_count != null && <span className="text-[10px] text-muted-foreground">{fmtCount(t.row_count)} rows</span>}
                    <button onClick={(e) => { e.stopPropagation(); askDashboard(t.table); }} title="Overview dashboard" className="rounded p-0.5 text-muted-foreground hover:bg-secondary hover:text-[#2DD4BF]">
                      <BarChart3 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                  <div className="px-2.5 pb-2" onClick={(e) => e.stopPropagation()}>
                    {colsOpen.has(t.table) ? (
                      <div className="flex flex-wrap gap-1">
                        {t.schema.map((c) => (
                          <span key={c.name} className="rounded bg-secondary px-1.5 py-0.5 text-[10px] text-muted-foreground">{c.name}</span>
                        ))}
                        {t.schema.length > 0 && (
                          <button onClick={() => toggleCols(t.table)} className="rounded px-1.5 py-0.5 text-[10px] font-medium text-[#2DD4BF] hover:bg-secondary">{T.collapseCols}</button>
                        )}
                      </div>
                    ) : (
                      <button
                        onClick={() => toggleCols(t.table)}
                        className="inline-flex items-center gap-1 rounded px-1 py-0.5 text-[10px] text-muted-foreground hover:text-foreground"
                      >
                        <span className="font-medium">{t.schema.length}</span> {T.columns}
                        <ChevronRight className="h-3 w-3" />
                      </button>
                    )}
                  </div>
                  {openTable === t.table && (
                    <div className="border-t border-border p-2" onClick={(e) => e.stopPropagation()}>
                      {sampleLoading ? (
                        <div className="flex items-center gap-2 py-2 text-[11px] text-muted-foreground"><Loader2 className="h-3 w-3 animate-spin" /> {T.loadingSample}</div>
                      ) : sample ? <SampleTable columns={sample.columns} rows={sample.rows} /> : <div className="py-2 text-[11px] text-muted-foreground">{T.noSample}</div>}
                    </div>
                  )}
                </div>
                );
              })}
            </div>
          )}
        </div>

        {/* knowledge sources */}
        <div className="border-t border-white/5">
          <div className="flex items-center gap-2 px-4 py-3">
            <button onClick={() => setKnowledgeOpen((value) => !value)} className="flex items-center gap-1.5 text-xs font-bold text-slate-200">
              {knowledgeOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
              <BrainCircuit className="h-4 w-4 text-violet-300" /> {T.knowledgeTitle}
            </button>
            <span className="rounded-full bg-violet-300/10 px-2 py-0.5 text-[9px] font-black text-violet-200">{knowledge.length}</span>
            <input
              ref={knowledgeInputRef}
              type="file"
              multiple
              accept=".pdf,.docx,.md,.markdown,.txt,.json,.html,.htm"
              className="hidden"
              onChange={(event) => { if (event.target.files?.length) uploadKnowledge(event.target.files); event.target.value = ""; }}
            />
            <button onClick={() => knowledgeInputRef.current?.click()} disabled={knowledgeBusy} className="ml-auto rounded-lg border border-violet-300/20 bg-violet-300/5 p-1.5 text-violet-200 hover:bg-violet-300/10">
              {knowledgeBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
            </button>
          </div>
          {knowledgeOpen && (
            <div className="max-h-44 space-y-1 overflow-y-auto px-3 pb-3">
              {knowledge.length === 0 ? (
                <button onClick={() => knowledgeInputRef.current?.click()} className="w-full rounded-xl border border-dashed border-violet-300/20 px-3 py-3 text-left text-[10px] leading-relaxed text-slate-500 hover:border-violet-300/40 hover:text-slate-300">
                  {T.knowledgeEmpty}
                </button>
              ) : knowledge.map((source) => (
                <div key={source.source_id} className="group flex items-center gap-2 rounded-lg border border-white/5 bg-white/[0.025] px-2.5 py-2">
                  <BookOpen className="h-3.5 w-3.5 shrink-0 text-violet-300" />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-[11px] font-semibold text-slate-200">{source.name}</div>
                    <div className="text-[9px] uppercase tracking-wide text-slate-600">{source.kind} · {Math.max(1, Math.round(source.characters / 1000))}k chars</div>
                  </div>
                  <button onClick={async () => { await api.daKnowledgeDelete(source.source_id); refreshKnowledge(); }} className="opacity-0 text-slate-600 hover:text-red-400 group-hover:opacity-100"><X className="h-3 w-3" /></button>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* reusable compiled workflows */}
        <div className="border-t border-white/5">
          <button onClick={() => setPlaybooksOpen((value) => !value)} className="flex w-full items-center gap-2 px-4 py-3 text-left text-xs font-bold text-slate-200">
            {playbooksOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
            <Network className="h-4 w-4 text-cyan-300" /> {T.playbooks}
            <span className="ml-auto rounded-full bg-cyan-300/10 px-2 py-0.5 text-[9px] font-black text-cyan-200">{playbooks.length}</span>
          </button>
          {playbooksOpen && (
            <div className="max-h-40 space-y-1 overflow-y-auto px-3 pb-3">
              {playbooks.length === 0 ? (
                <div className="rounded-xl border border-dashed border-cyan-300/15 px-3 py-3 text-[10px] leading-relaxed text-slate-600">{T.playbookEmpty}</div>
              ) : playbooks.slice(0, 8).map((workflow) => (
                <button key={workflow.workflow_id} onClick={() => setInput(workflow.goal)} className="group w-full rounded-lg border border-white/5 bg-white/[0.025] px-2.5 py-2 text-left hover:border-cyan-300/20 hover:bg-cyan-300/5">
                  <div className="line-clamp-2 text-[10px] font-semibold leading-snug text-slate-300">{workflow.goal}</div>
                  <div className="mt-1 flex items-center gap-2 text-[9px] uppercase tracking-wide text-slate-600"><span>{workflow.nodes} nodes</span><span>{workflow.layers} layers</span>{workflow.validation_ok && <ShieldCheck className="ml-auto h-3 w-3 text-emerald-400" />}</div>
                </button>
              ))}
            </div>
          )}
        </div>

        {/* skills library (moved here from the top-nav dropdown) */}
        <div className="border-t border-white/5">
          <button onClick={() => setSkillsOpen((value) => !value)} className="flex w-full items-center gap-2 px-4 py-3 text-left text-xs font-bold text-slate-200">
            {skillsOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
            <Zap className="h-4 w-4 text-amber-300" /> {T.skillsTitle}
            <span className="ml-auto rounded-full bg-amber-300/10 px-2 py-0.5 text-[9px] font-black text-amber-200">{skills.filter((s) => s.enabled).length}/{skills.length}</span>
          </button>
          {skillsOpen && (
            <div className="px-3 pb-3">
              {skills.length > 8 && (
                <input
                  value={skillSearch}
                  onChange={(e) => setSkillSearch(e.target.value)}
                  placeholder={T.skillsSearch}
                  className="mb-2 w-full rounded-md border border-white/7 bg-white/[0.025] px-2 py-1 text-[11px] text-slate-200 placeholder:text-slate-600"
                />
              )}
              <div className="max-h-52 space-y-1 overflow-y-auto">
                {skills.length === 0 ? (
                  <div className="rounded-xl border border-dashed border-amber-300/15 px-3 py-3 text-[10px] leading-relaxed text-slate-600">{T.skillsEmpty}</div>
                ) : skills
                  .filter((s) => !skillSearch || s.name.toLowerCase().includes(skillSearch.toLowerCase()) || (s.description || "").toLowerCase().includes(skillSearch.toLowerCase()))
                  .map((skill) => {
                    const isFresh = skill.name === freshSkill;
                    return (
                      <div key={skill.name} className={`group flex items-start gap-2 rounded-lg border px-2.5 py-2 ${isFresh ? "border-amber-300/50 bg-amber-300/10" : "border-white/5 bg-white/[0.025]"}`}>
                        <Zap className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${skill.enabled ? "text-amber-300" : "text-slate-600"}`} />
                        <button onClick={() => openSkill(skill.name)} title={T.skillView} className="min-w-0 flex-1 text-left">
                          <div className="flex items-center gap-1.5">
                            <span className="truncate text-[11px] font-semibold text-slate-200 group-hover:text-amber-200">{skill.name}</span>
                            {viewSkillLoading === skill.name && <Loader2 className="h-3 w-3 shrink-0 animate-spin text-amber-300" />}
                            {isFresh && <span className="shrink-0 rounded-full bg-amber-400/20 px-1.5 text-[8px] font-black uppercase text-amber-200">{T.skillNew}</span>}
                          </div>
                          <div className="line-clamp-2 text-[9px] leading-tight text-slate-500">{skill.description}</div>
                        </button>
                        <button
                          onClick={async (e) => { e.stopPropagation(); await api.toggleSkill(skill.name, !skill.enabled); refreshSkills(); }}
                          title={skill.enabled ? T.skillDisable : T.skillEnable}
                          className={`mt-0.5 shrink-0 rounded-full px-1.5 py-0.5 text-[8px] font-bold uppercase ${skill.enabled ? "bg-emerald-400/15 text-emerald-300" : "bg-white/5 text-slate-500"}`}
                        >
                          {skill.enabled ? T.skillOn : T.skillOff}
                        </button>
                      </div>
                    );
                  })}
              </div>
            </div>
          )}
        </div>
      </aside>

      <div onMouseDown={() => setSidebarResizing(true)} className="data-resizer group relative z-20 flex h-full w-1.5 shrink-0 cursor-col-resize items-center justify-center">
        <div className="h-full w-px bg-border group-hover:bg-[#2DD4BF]" />
      </div>

      {/* Center: chat */}
      <div className="data-center flex min-w-0 flex-1 flex-col bg-[radial-gradient(circle_at_50%_-10%,rgba(47,117,255,.17),transparent_38%)]">
        <div className="mission-topbar flex items-center gap-3 border-b border-white/5 px-5 py-3">
          <button onClick={() => setMobileSidebarOpen(true)} className="data-mobile-menu hidden rounded-xl border border-cyan-300/15 bg-cyan-300/5 p-2 text-cyan-200"><Database className="h-4 w-4" /></button>
          <span className="relative flex h-9 w-9 items-center justify-center rounded-xl border border-cyan-300/20 bg-cyan-300/10 text-cyan-200"><Orbit className="h-5 w-5" /><span className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full border-2 border-[#212e42] bg-emerald-400" /></span>
          <div>
            <div className="flex items-center gap-2"><span className="font-black tracking-tight text-slate-50">{T.missionControl}</span><span className="rounded-full border border-emerald-300/20 bg-emerald-300/10 px-2 py-0.5 text-[9px] font-bold uppercase tracking-[.14em] text-emerald-300">{T.missionOnline}</span></div>
            <div className="text-[10px] uppercase tracking-[.16em] text-slate-500">{sourceMode === "table" ? T.subtitleTable : T.subtitleAsk}</div>
          </div>
          <div className="ml-4 hidden items-center gap-2 lg:flex">
            {[{ icon: Database, value: tables.length, label: T.missionTables }, { icon: BrainCircuit, value: knowledge.length, label: T.missionKnowledge }, { icon: ShieldCheck, value: selectedTables.length || "all", label: T.missionScope }].map((item) => (
              <div key={item.label} className="flex items-center gap-1.5 rounded-full border border-white/7 bg-white/[0.025] px-2.5 py-1 text-[10px] text-slate-400"><item.icon className="h-3 w-3 text-cyan-300" /><strong className="text-slate-200">{item.value}</strong>{item.label}</div>
            ))}
          </div>
          {active?.artifacts.length ? (
            <button onClick={() => { setArtifactOpen(true); setShowArtifactList(true); }} className="ml-auto inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground hover:bg-secondary hover:text-foreground">
              <Files className="h-3.5 w-3.5" /> {T.artifacts} · {active.artifacts.length}
            </button>
          ) : null}
        </div>

        <div className="relative flex-1 space-y-4 overflow-y-auto p-5">
          {messages.length === 0 && (
            <div className="mission-hero mx-auto mt-[8vh] max-w-3xl text-center">
              <div className="mx-auto mb-5 flex h-16 w-16 items-center justify-center rounded-2xl border border-cyan-300/20 bg-gradient-to-br from-blue-500/20 to-cyan-300/10 shadow-[0_0_60px_rgba(79,140,255,.2)]"><Network className="h-8 w-8 text-cyan-200" /></div>
              <div className="mb-2 text-[10px] font-black uppercase tracking-[.28em] text-cyan-300">{T.missionKicker}</div>
              <h1 className="mission-title text-balance text-4xl font-black tracking-[-.05em] text-white md:text-6xl">{T.missionHero}</h1>
              <p className="mx-auto mt-5 max-w-2xl text-sm leading-7 text-slate-400">{T.missionBody}</p>
              <div className="mt-7 grid gap-3 text-left sm:grid-cols-3">
                {[{ icon: Zap, title: T.missionCompile, body: T.missionCompileBody }, { icon: ShieldCheck, title: T.missionValidate, body: T.missionValidateBody }, { icon: FileSearch, title: T.missionSynthesize, body: T.missionSynthesizeBody }].map((card) => (
                  <div key={card.title} className="rounded-2xl border border-white/7 bg-white/[0.025] p-4 backdrop-blur"><card.icon className="h-4 w-4 text-cyan-300" /><div className="mt-3 text-sm font-bold text-slate-100">{card.title}</div><div className="mt-1 text-[11px] text-slate-500">{card.body}</div></div>
                ))}
              </div>
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`group flex gap-3 ${m.role === "user" ? "justify-end" : ""}`}>
              {m.role === "assistant" && (
                <BotAvatar size={28} />
              )}
              <div className={`min-w-0 max-w-[78%] ${m.role === "user" ? "" : "flex-1"}`}>
                {m.role === "assistant" && m.workflow && <WorkflowMission workflow={m.workflow} trace={m.trace} running={m.running} />}
                <div className={`rounded-2xl px-4 py-2.5 text-sm ${m.role === "user" ? "bg-gradient-to-br from-blue-500 to-cyan-500 text-white shadow-[0_10px_30px_rgba(43,128,255,.2)]" : "border border-white/7 bg-[#212e42]/90 text-slate-200 shadow-[0_14px_45px_rgba(1,7,18,.12)]"}`}>
                  {m.role === "assistant" ? (
                    m.content ? <Markdown content={m.content} /> : m.running ? (
                      <span className="flex items-center gap-2 text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" /> {T.thinking}</span>
                    ) : null
                  ) : <span className="whitespace-pre-wrap">{m.content}</span>}
                </div>
                {m.role === "assistant" && !!m.content && (
                  <button onClick={() => copyMessage(i, m.content)} className="mt-1 inline-flex items-center gap-1 text-[11px] text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 hover:text-foreground">
                    {copiedIdx === i ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}{copiedIdx === i ? T.copied : T.copy}
                  </button>
                )}
                {m.role === "assistant" && (m.trace?.length || m.running) && (
                  <TracePanel trace={m.trace} running={m.running} sourceMode={sourceMode} />
                )}
                {m.role === "assistant" && m.clarify && (
                  <ClarifyCard prompt={m.clarify} onAnswer={(ans) => answerClarify(activeId, i, m.clarify!, ans)} T={T} />
                )}
              </div>
              {m.role === "user" && (
                <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-secondary"><User className="h-4 w-4" /></div>
              )}
            </div>
          ))}
          <div ref={chatEndRef} />
        </div>

        {/* composer */}
        <div className="border-t border-white/5 bg-[#1e2a3d]/90 p-3 backdrop-blur-xl">
          {suggestions.length > 0 && messages.length === 0 && (
            <div className="mb-2.5">
              <div className="mb-1.5 flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wide text-cyan-300/80">
                <Sparkles className="h-3 w-3" /> {T.suggestedQuestions}
              </div>
              <div className="flex flex-wrap gap-1.5">
                {suggestions.map((q) => (
                  <button
                    key={q}
                    onClick={() => { setInput(q); setSuggestions([]); }}
                    className="rounded-full border border-cyan-300/20 bg-cyan-300/5 px-2.5 py-1 text-left text-[11px] text-slate-200 hover:border-cyan-300/45 hover:bg-cyan-300/10"
                  >
                    {q}
                  </button>
                ))}
              </div>
            </div>
          )}
          {draftWorkflow && (
            <div className="mb-3 rounded-2xl border border-cyan-300/15 bg-[#212e42]/95 p-3 shadow-[0_20px_70px_rgba(0,0,0,.22)]">
              <div className="mb-1 flex items-center gap-2 text-[10px] font-black uppercase tracking-[.18em] text-cyan-300"><Network className="h-3.5 w-3.5" />{T.missionReady}</div>
              <div className="mb-2 text-[11px] text-slate-400">{T.approveHint}</div>
              <WorkflowMission workflow={draftWorkflow} editable onRemoveNode={removeDraftNode} />
              <div className="flex justify-end gap-2">
                <button onClick={() => { setDraftWorkflow(null); setDraftText(""); }} className="rounded-lg border border-white/10 px-3 py-1.5 text-xs text-slate-400 hover:bg-white/5">{T.dismissPlan}</button>
                <button onClick={() => void send(draftText, draftWorkflow)} className="mission-primary inline-flex items-center gap-1.5 rounded-lg px-4 py-1.5 text-xs font-black text-[#041817]"><Zap className="h-3.5 w-3.5" />{T.approvePlan}</button>
              </div>
            </div>
          )}
          {selectedTables.length > 0 && (
            <div className="mb-2 flex flex-wrap items-center gap-1.5">
              <span className="text-[11px] text-muted-foreground">{T.askingAbout}:</span>
              {selectedTables.map((tbl) => (
                <span key={tbl} className="inline-flex items-center gap-1 rounded-full border border-[#2DD4BF]/40 bg-[#2DD4BF]/10 px-2 py-0.5 text-[11px] font-medium text-[#0d9488]">
                  <Table2 className="h-3 w-3" />
                  <span className="max-w-[160px] truncate font-mono">{tbl}</span>
                  <button onClick={() => toggleSelect(tbl)} className="hover:text-red-500"><X className="h-3 w-3" /></button>
                </span>
              ))}
              <button onClick={() => setSelectedTables([])} className="text-[11px] text-muted-foreground hover:text-foreground underline-offset-2 hover:underline">
                {T.clearSelection}
              </button>
            </div>
          )}
          <div className="flex items-end gap-2">
            <div className="relative flex-1">
              <textarea value={input} onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); return; }
                  // Ctrl/⌘+C clears the composer when nothing is selected
                  // (so normal copy-of-selection still works).
                  if ((e.ctrlKey || e.metaKey) && (e.key === "c" || e.key === "C")) {
                    const el = e.currentTarget;
                    if (el.selectionStart === el.selectionEnd && input) { e.preventDefault(); setInput(""); }
                  }
                }}
                rows={2} placeholder={T.askPlaceholder} className="w-full resize-none rounded-lg border border-border bg-background px-3 py-2 pr-8 text-sm" />
              {input && (
                <button
                  onClick={() => setInput("")}
                  title={T.clearInput}
                  className="absolute right-1.5 top-1.5 inline-flex h-5 w-5 items-center justify-center rounded-full text-muted-foreground hover:bg-secondary hover:text-foreground"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
            <input ref={fileInputRef} type="file" multiple accept=".csv,.tsv,.txt,.parquet,.xlsx,.xls,.sqlite,.sqlite3,.db,.db3,.duckdb" className="hidden"
              onChange={(e) => { if (e.target.files?.length) uploadAndConnect(e.target.files); e.target.value = ""; }} />
            <button onClick={() => fileInputRef.current?.click()} disabled={connecting} title="Upload a data file" className="inline-flex h-10 items-center justify-center rounded-lg border border-border bg-background px-3 text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-50">
              <Paperclip className="h-4 w-4" />
            </button>
            <button onClick={compileDraft} disabled={!input.trim() || busy || compiling} title={!input.trim() ? T.compilePlanHint : T.compilePlan} className="inline-flex h-10 items-center gap-1.5 rounded-lg border border-cyan-300/20 bg-cyan-300/5 px-3 text-xs font-bold text-cyan-200 hover:bg-cyan-300/10 disabled:opacity-40">
              {compiling ? <Loader2 className="h-4 w-4 animate-spin" /> : <Network className="h-4 w-4" />}<span className="hidden sm:inline">{T.compilePlan}</span>
            </button>
            {busy ? (
              <button onClick={stop} title="Stop" className="inline-flex h-10 items-center gap-1.5 rounded-lg border border-border bg-background px-4 text-sm font-medium text-foreground hover:bg-secondary">
                <Square className="h-4 w-4" /> {T.stop}
              </button>
            ) : (
              <button onClick={() => send()} disabled={!input.trim()} className="inline-flex h-10 items-center gap-1.5 rounded-lg bg-[#2DD4BF] px-4 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50">
                <Send className="h-4 w-4" /> {T.send}
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Right: artifact panel */}
      {artifactOpen && artifact && (
        <>
          <div onMouseDown={() => setArtifactResizing(true)} className="group relative z-20 flex h-full w-1.5 shrink-0 cursor-col-resize items-center justify-center">
            <div className="h-full w-px bg-border group-hover:bg-[#2DD4BF]" />
          </div>
          <aside style={{ width: artifactWidth }} className="data-artifact flex h-full shrink-0 flex-col border-l border-border bg-card">
            {/* artifact库 header w/ list toggle */}
            <div className="flex items-center gap-2 border-b border-border px-4 py-3">
              <button onClick={() => setShowArtifactList((v) => !v)} title="All artifacts" className="rounded p-1 text-muted-foreground hover:bg-secondary hover:text-foreground">
                <Files className="h-4 w-4" />
              </button>
              {artifactIcon(artifact.kind)}
              <span className="min-w-0 flex-1 truncate text-sm font-semibold">{artifact.title}</span>
              <span className="rounded bg-secondary px-1.5 py-0.5 text-[10px] uppercase text-muted-foreground">{artifact.kind}</span>
              <button onClick={() => api.downloadWorkspaceFile(artifact.file_id, artifact.workspace_file)} title="Download" className="rounded p-1 text-muted-foreground hover:bg-secondary hover:text-foreground"><Download className="h-4 w-4" /></button>
              <button onClick={() => removeArtifact(artifact)} title="Delete" className="rounded p-1 text-muted-foreground hover:bg-secondary hover:text-red-500"><Trash2 className="h-4 w-4" /></button>
              <button onClick={() => setArtifactOpen(false)} title="Close" className="rounded p-1 text-muted-foreground hover:bg-secondary hover:text-foreground"><X className="h-4 w-4" /></button>
            </div>

            {/* artifact list (库) */}
            {showArtifactList && (
              <div className="border-b border-border p-2">
                <div className="mb-1 px-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{T.artifactsInChat}</div>
                <div className="max-h-48 space-y-1 overflow-y-auto">
                  {active.artifacts.map((a) => (
                    <button key={a.file_id} onClick={() => { updateActive((c) => ({ ...c, activeArtifactId: a.file_id })); setShowArtifactList(false); }}
                      className={`flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-left text-xs ${a.file_id === artifact.file_id ? "bg-secondary" : "hover:bg-secondary/60"}`}>
                      {artifactIcon(a.kind)}
                      <span className="min-w-0 flex-1 truncate">{a.title}</span>
                      <span className="text-[9px] uppercase text-muted-foreground">{a.kind}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}

            <div className="min-h-0 flex-1">
              {artifact.kind === "excel" ? (
                <div className="flex h-full flex-col">
                  {artifactSheets && artifactSheets.length > 0 ? (
                    <>
                      {artifactSheets.length > 1 && (
                        <div className="flex flex-wrap gap-1 border-b border-border px-3 py-2">
                          {artifactSheets.map((s, i) => (
                            <button key={s.name + i} onClick={() => setActiveSheet(i)} className={`rounded-md px-2.5 py-1 text-xs font-medium ${i === activeSheet ? "bg-[#2DD4BF] text-white" : "text-muted-foreground hover:bg-secondary"}`}>{s.name}</button>
                          ))}
                        </div>
                      )}
                      <div className="min-h-0 flex-1 overflow-auto p-3">
                        {(() => {
                          const sh = artifactSheets[activeSheet];
                          if (!sh) return null;
                          const cols = sh.columns.map((c) => c.name);
                          return (
                            <>
                              <div className="mb-2 text-xs text-muted-foreground">{sh.name} · {sh.row_count ?? "?"} {T.rows} · {T.previewFirst} {sh.sample.length} {T.rows}</div>
                              <div className="overflow-x-auto rounded-lg border border-border">
                                <table className="w-full text-xs">
                                  <thead><tr className="border-b border-border bg-secondary/50">{cols.map((c) => <th key={c} className="px-2.5 py-1.5 text-left font-semibold">{c}</th>)}</tr></thead>
                                  <tbody>
                                    {sh.sample.map((row, ri) => (
                                      <tr key={ri} className="border-b border-border last:border-0">
                                        {cols.map((c) => <td key={c} className="px-2.5 py-1.5 tabular-nums">{row[c] == null ? <span className="text-muted-foreground">·</span> : String(row[c])}</td>)}
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>
                            </>
                          );
                        })()}
                      </div>
                    </>
                  ) : <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground"><Loader2 className="mr-2 h-4 w-4 animate-spin" /> {T.renderingPreview}</div>}
                  <div className="border-t border-border p-3">
                    <button onClick={() => api.downloadWorkspaceFile(artifact.file_id, artifact.workspace_file)} className="flex w-full items-center justify-center gap-2 rounded-lg bg-[#2DD4BF] px-5 py-2.5 text-sm font-medium text-white hover:opacity-90"><Download className="h-4 w-4" /> {T.downloadXlsx}</button>
                    <div className="mt-1.5 text-center text-[11px] text-muted-foreground">{T.xlsxPreviewNote}</div>
                  </div>
                </div>
              ) : artifactHtml ? (
                artifact.kind === "story" ? (
                  // Stories may embed per-slide narration <audio> with RELATIVE src;
                  // load via the file URL (not srcDoc) so those relative audio URLs
                  // resolve against /api/da/download/ and the narration plays.
                  <iframe title={artifact.title ?? "artifact"} src={`/api/da/view/${encodeURIComponent(artifact.workspace_file)}`} sandbox="allow-same-origin allow-scripts" className="h-full w-full border-0 bg-white" />
                ) : (
                  <iframe title={artifact.title ?? "artifact"} srcDoc={artifactHtml} sandbox="allow-same-origin allow-scripts" className="h-full w-full border-0 bg-white" />
                )
              ) : (
                <div className="flex h-full items-center justify-center text-sm text-muted-foreground"><Loader2 className="mr-2 h-4 w-4 animate-spin" /> {T.rendering}</div>
              )}
            </div>
          </aside>
        </>
      )}

      {/* Skill viewer modal — full SKILL.md rendered */}
      {viewSkill && (
        <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/50 p-6 backdrop-blur-sm" onClick={() => setViewSkill(null)}>
          <div className="flex max-h-[82vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-amber-300/20 bg-[#1a2536] shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-2 border-b border-white/10 px-4 py-3">
              <Zap className="h-4 w-4 shrink-0 text-amber-300" />
              <span className="truncate text-sm font-bold text-slate-100">{viewSkill.name}</span>
              {viewSkill.path && <span className="hidden truncate font-mono text-[10px] text-slate-500 sm:inline">{viewSkill.path}</span>}
              <button onClick={() => setViewSkill(null)} className="ml-auto rounded p-1 text-slate-400 hover:bg-white/5 hover:text-slate-200"><X className="h-4 w-4" /></button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4 text-sm text-slate-200">
              <Markdown content={viewSkill.content || viewSkill.description || "(empty skill)"} />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
