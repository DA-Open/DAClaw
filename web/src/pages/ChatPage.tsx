import { useEffect, useMemo, useRef, useState } from "react";
import {
  Bot,
  ChevronDown,
  ChevronRight,
  Copy,
  FileText,
  Loader2,
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
  Paperclip,
  Plus,
  Send,
  Trash2,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import type { ChatAttachment, ChatRunEvent, ChatStreamEvent, ChatStreamMessage, ChatTraceEvent } from "@/lib/api";
import { Markdown } from "@/components/Markdown";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

type ChatRole = "user" | "assistant";

interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  attachments?: ChatAttachment[];
  trace?: ChatTraceEvent[];
  running?: boolean;
  runId?: string;
  lastSeq?: number;
}

interface Conversation {
  id: string;
  title: string;
  sessionId?: string | null;
  updatedAt: number;
  messages: ChatMessage[];
}

const STORAGE_KEY = "daclaw-web-chat-conversations";
const TRACE_EVENT_LIMIT = 60;
const TRACE_TEXT_LIMIT = 3000;
const PERSISTED_MESSAGE_TEXT_LIMIT = 120_000;

function isMethodNotAllowedError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return message.startsWith("405:");
}

function makeId(prefix = "id"): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function newConversation(): Conversation {
  return {
    id: makeId("chat"),
    title: "New chat",
    updatedAt: Date.now(),
    messages: [],
  };
}

function loadConversations(): Conversation[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]") as Conversation[];
    if (Array.isArray(parsed) && parsed.length > 0) {
      return parsed.slice(0, 30).map((conversation) => ({
        ...conversation,
        messages: (conversation.messages || []).map((message) => ({
          ...message,
          running: Boolean(message.running && message.runId),
          lastSeq: typeof message.lastSeq === "number" ? message.lastSeq : -1,
          trace: sanitizeTrace(message.trace),
        })),
      }));
    }
  } catch {
    // Ignore corrupted local UI state.
  }
  return [newConversation()];
}

function persistableConversations(conversations: Conversation[]): Conversation[] {
  return conversations.slice(0, 30).map((conversation) => ({
    ...conversation,
    messages: conversation.messages.map((message) => ({
      ...message,
      content: truncatePersistedMessage(message.content),
      running: Boolean(message.running && message.runId),
      lastSeq: typeof message.lastSeq === "number" ? message.lastSeq : -1,
      trace: sanitizeTrace(message.trace),
    })),
  }));
}

function isTerminalChatRunStatus(status: string | undefined): boolean {
  return status === "completed" || status === "failed";
}

function delay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const timeout = window.setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timeout);
        reject(new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });
}

function titleFrom(text: string): string {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean ? clean.slice(0, 48) : "File analysis";
}

function normalizeMessageText(value: string): string {
  return value.replace(/\r\n/g, "\n").trim();
}

function formatBytes(size: number): string {
  if (!Number.isFinite(size) || size <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = size;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = unit === 0 || value >= 10 ? 0 : 1;
  return `${value.toFixed(digits)} ${units[unit]}`;
}

function truncateText(value: string | undefined, limit = TRACE_TEXT_LIMIT): string | undefined {
  if (!value || value.length <= limit) return value;
  return `${value.slice(0, limit)}\n... [truncated ${value.length - limit} chars]`;
}

function truncatePersistedMessage(value: string): string {
  if (value.length <= PERSISTED_MESSAGE_TEXT_LIMIT) return value;
  return `${value.slice(0, PERSISTED_MESSAGE_TEXT_LIMIT)}\n\n... [conversation content truncated for browser storage; the original run completed successfully]`;
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
  };
}

function shouldKeepTraceEvent(event: ChatTraceEvent): boolean {
  if (event.type === "assistant_delta" || event.type === "reasoning" || event.type === "step" || event.type === "run_started") {
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

function appendTraceEvent(trace: ChatTraceEvent[] = [], event: ChatTraceEvent): ChatTraceEvent[] {
  if (!shouldKeepTraceEvent(event)) return trace;
  return [...trace, sanitizeTraceEvent(event)].slice(-TRACE_EVENT_LIMIT);
}

function sanitizeTrace(trace: ChatTraceEvent[] = []): ChatTraceEvent[] {
  return trace.filter(shouldKeepTraceEvent).map(sanitizeTraceEvent).slice(-TRACE_EVENT_LIMIT);
}

function TracePanel({ trace = [], running = false }: { trace?: ChatTraceEvent[]; running?: boolean }) {
  const [open, setOpen] = useState(running);
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
        <span className="font-medium text-foreground">DAClaw 调用过程</span>
        <span className="ml-auto">{running ? "运行中" : `${toolCount} 次工具调用`}</span>
      </button>
      {open && (
        <div className="border-t border-border">
          <div className="divide-y divide-border">
            {items.map((event, index) => (
              <TraceEvent key={`${event.type}-${index}`} event={event} />
            ))}
            {running && <TraceActiveRow trace={trace} visibleItems={items} />}
          </div>
        </div>
      )}
    </div>
  );
}

function TraceActiveRow({ trace, visibleItems }: { trace: ChatTraceEvent[]; visibleItems: ChatTraceEvent[] }) {
  const lastVisible = visibleItems[visibleItems.length - 1];
  const lastRaw = trace[trace.length - 1];
  let label = "正在启动 DAClaw";

  if (lastVisible?.type === "tool_start") {
    label = `正在运行 ${lastVisible.summary || lastVisible.name || "工具"}`;
  } else if (lastRaw?.type === "assistant_delta") {
    label = "正在写入回复";
  } else if (lastVisible?.type === "tool_complete") {
    label = "正在分析结果并决定下一步";
  } else if (lastRaw?.type === "step" || lastRaw?.type === "run_started") {
    label = "正在规划下一步";
  }

  return (
    <div className="flex items-center gap-3 px-4 py-2.5 text-xs text-muted-foreground">
      <span className="w-3">
        <Loader2 className="h-3 w-3 animate-spin" />
      </span>
      <span className="rounded-full bg-background px-2 py-0.5 text-[0.68rem] font-medium">进行中</span>
      <span className="min-w-0 flex-1 truncate text-foreground">{label}</span>
    </div>
  );
}

function TraceEvent({ event }: { event: ChatTraceEvent }) {
  const [open, setOpen] = useState(Boolean(event.is_error || event.type === "error"));
  const label = formatTraceLabel(event);
  const meta = event.duration ? `${event.duration.toFixed(1)}s` : event.is_error ? "error" : "";
  const hasBody = Boolean(event.text || event.message || event.preview || event.result || event.args);

  return (
    <div>
      <button
        type="button"
        className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-xs hover:bg-secondary/80 disabled:cursor-default"
        onClick={() => hasBody && setOpen((value) => !value)}
        disabled={!hasBody}
      >
        {hasBody ? (
          open ? <ChevronDown className="h-3 w-3 text-muted-foreground" /> : <ChevronRight className="h-3 w-3 text-muted-foreground" />
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
          {traceStatusLabel(event)}
        </span>
        <span className="min-w-0 flex-1 truncate text-foreground">{label}</span>
        {meta && <span className="text-muted-foreground">{meta}</span>}
      </button>
      {open && hasBody && (
        <div className="space-y-2 px-6 pb-3 text-xs">
          {event.message && <TraceText label="status" value={event.message} />}
          {event.preview && <TraceText label="preview" value={event.preview} />}
          {event.args && Object.keys(event.args).length > 0 && (
            <TraceText label="args" value={JSON.stringify(event.args, null, 2)} mono />
          )}
          {event.result && <TraceText label="result" value={event.result} mono />}
        </div>
      )}
    </div>
  );
}

function formatTraceLabel(event: ChatTraceEvent): string {
  const name = event.name || event.event || "DAClaw";
  const target = event.summary || name;
  if (event.type === "tool_start") return `运行 ${target}`;
  if (event.type === "tool_complete") return `完成 ${target}`;
  if (event.type === "tool_progress") return `${target} 出现问题`;
  if (event.type === "error") return "请求失败";
  return event.message || name;
}

function traceStatusLabel(event: ChatTraceEvent): string {
  if (event.type === "tool_start") return "调用";
  if (event.type === "tool_complete") return "完成";
  if (event.type === "tool_progress") return "工具";
  if (event.type === "error" || event.is_error) return "错误";
  return "状态";
}

function TraceText({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="grid gap-1 sm:grid-cols-[5rem_1fr]">
      <span className="text-[0.65rem] uppercase tracking-wide text-muted-foreground">{label}</span>
      <pre className={cn("whitespace-pre-wrap break-words text-muted-foreground", mono && "font-mono-ui")}>{value}</pre>
    </div>
  );
}

function AttachmentChip({ file }: { file: ChatAttachment }) {
  return (
    <div className="flex min-w-0 items-center gap-2 rounded-2xl border border-border bg-background/80 px-3 py-2">
      <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium leading-5 text-foreground">{file.name}</div>
        <div className="text-xs leading-4 text-muted-foreground">{formatBytes(file.size)}</div>
      </div>
    </div>
  );
}

function MessageBlock({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  const hasAttachments = Boolean(message.attachments?.length);
  const copyMessage = () => {
    if (!message.content.trim()) return;
    void navigator.clipboard?.writeText(message.content);
  };

  return (
    <div className={cn("flex w-full py-5", isUser ? "justify-end" : "justify-start")}>
      <div className={cn("flex w-full gap-3", isUser ? "max-w-[780px] justify-end" : "max-w-[980px] justify-start")}>
        {!isUser && (
          <div className="mt-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-foreground text-background">
            <Bot className="h-4 w-4" />
          </div>
        )}
        <div className={cn("min-w-0", isUser ? "flex w-full max-w-full justify-end" : "flex-1")}>
          {isUser ? (
            <div
              className={cn(
                "w-fit max-w-full rounded-3xl bg-secondary px-4 py-3 text-[15px] leading-7 text-foreground",
                hasAttachments && "min-w-[18rem]",
              )}
            >
              {hasAttachments && (
                <div className={cn("space-y-2", message.content && "mb-2")}>
                  {message.attachments?.map((file) => (
                    <AttachmentChip key={file.id} file={file} />
                  ))}
                </div>
              )}
              {message.content && (
                <div className="whitespace-pre-wrap [line-break:strict] [overflow-wrap:normal] [word-break:normal]">
                  {message.content}
                </div>
              )}
            </div>
          ) : (
            <>
              <TracePanel trace={message.trace} running={message.running} />
              <div className="group/message mt-4 text-[15px] leading-7 text-foreground">
                <div className="select-text">
                  <Markdown content={message.content || (message.running ? "..." : "")} />
                </div>
                {message.content.trim() && (
                  <button
                    type="button"
                    className="mt-2 inline-flex h-7 items-center gap-1 rounded-full border border-border px-2 text-xs text-muted-foreground opacity-0 transition-opacity hover:text-foreground group-hover/message:opacity-100 focus:opacity-100"
                    onClick={copyMessage}
                  >
                    <Copy className="h-3.5 w-3.5" />
                    Copy
                  </button>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export default function ChatPage() {
  const [conversations, setConversations] = useState<Conversation[]>(loadConversations);
  const [activeId, setActiveId] = useState(() => conversations[0]?.id ?? "");
  const [input, setInput] = useState("");
  const [pendingFiles, setPendingFiles] = useState<ChatAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [, setRunning] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    try {
      return localStorage.getItem("daclaw-chat-sidebar-collapsed") === "1";
    } catch {
      return false;
    }
  });
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const deltaBufferRef = useRef("");
  const deltaTimerRef = useRef<number | null>(null);
  const legacyStreamSeqRef = useRef(0);

  const active = useMemo(
    () => conversations.find((item) => item.id === activeId) ?? conversations[0],
    [activeId, conversations],
  );
  const activeRunning = Boolean(active?.messages.some((message) => message.running));

  useEffect(() => {
    const persist = () => {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(persistableConversations(conversations)));
    };
    const handle = window.setTimeout(() => {
      persist();
    }, 800);
    return () => {
      window.clearTimeout(handle);
      persist();
    };
  }, [conversations]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [active?.messages, activeRunning]);

  useEffect(() => {
    try {
      localStorage.setItem("daclaw-chat-sidebar-collapsed", sidebarCollapsed ? "1" : "0");
    } catch {
      // Ignore local UI persistence failures.
    }
  }, [sidebarCollapsed]);

  const updateConversation = (id: string, updater: (conversation: Conversation) => Conversation) => {
    setConversations((prev) =>
      prev.map((conversation) => (conversation.id === id ? updater(conversation) : conversation)),
    );
  };

  const updateActive = (updater: (conversation: Conversation) => Conversation) => {
    if (!active) return;
    updateConversation(active.id, updater);
  };

  const addConversation = () => {
    const created = newConversation();
    setConversations((prev) => [created, ...prev]);
    setActiveId(created.id);
    setSidebarOpen(false);
  };

  const deleteConversation = (id: string) => {
    setConversations((prev) => {
      const next = prev.filter((item) => item.id !== id);
      if (activeId === id) setActiveId(next[0]?.id ?? "");
      return next.length ? next : [newConversation()];
    });
  };

  const handleFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setUploading(true);
    setUploadError(null);
    try {
      const uploaded = await Promise.all(Array.from(files).map((file) => api.uploadChatFile(file)));
      setPendingFiles((prev) => [...prev, ...uploaded]);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setUploadError(message);
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const removePendingFile = (id: string) => {
    setPendingFiles((prev) => prev.filter((file) => file.id !== id));
  };

  const flushAssistantDelta = (conversationId: string, assistantId: string) => {
    const delta = deltaBufferRef.current;
    if (!delta) return;
    deltaBufferRef.current = "";
    updateConversation(conversationId, (conversation) => ({
      ...conversation,
      messages: conversation.messages.map((message) =>
        message.id === assistantId ? { ...message, content: message.content + delta } : message,
      ),
    }));
  };

  const scheduleAssistantDelta = (conversationId: string, assistantId: string, delta: string) => {
    deltaBufferRef.current += delta;
    if (deltaTimerRef.current !== null) return;
    deltaTimerRef.current = window.setTimeout(() => {
      deltaTimerRef.current = null;
      flushAssistantDelta(conversationId, assistantId);
    }, 120);
  };

  const applyRunEvent = (conversationId: string, assistantId: string, event: ChatRunEvent) => {
    if (event.type === "assistant_delta" && "delta" in event) {
      scheduleAssistantDelta(conversationId, assistantId, event.delta);
      updateConversation(conversationId, (conversation) => ({
        ...conversation,
        updatedAt: Date.now(),
        messages: conversation.messages.map((message) =>
          message.id === assistantId ? { ...message, runId: event.run_id, lastSeq: event.seq } : message,
        ),
      }));
      return;
    }

    if (event.type === "heartbeat") {
      return;
    }

    if (event.type === "done" && "final_response" in event) {
      if (deltaTimerRef.current !== null) {
        window.clearTimeout(deltaTimerRef.current);
        deltaTimerRef.current = null;
      }
      flushAssistantDelta(conversationId, assistantId);
      updateConversation(conversationId, (conversation) => ({
        ...conversation,
        sessionId: event.session_id ?? conversation.sessionId,
        updatedAt: Date.now(),
        messages: conversation.messages.map((message) =>
          message.id === assistantId
            ? {
                ...message,
                content: event.final_response || message.content,
                trace: sanitizeTrace(event.trace),
                running: false,
                runId: event.run_id,
                lastSeq: event.seq,
              }
            : message,
        ),
      }));
      return;
    }

    if (event.type === "error") {
      updateConversation(conversationId, (conversation) => ({
        ...conversation,
        updatedAt: Date.now(),
        messages: conversation.messages.map((message) =>
          message.id === assistantId
            ? {
                ...message,
                content: message.content || `DAClaw web chat failed: ${event.message}`,
                running: false,
                trace: appendTraceEvent(message.trace, event),
                runId: event.run_id,
                lastSeq: event.seq,
              }
            : message,
        ),
      }));
      return;
    }

    updateConversation(conversationId, (conversation) => ({
      ...conversation,
      updatedAt: Date.now(),
      messages: conversation.messages.map((message) =>
        message.id === assistantId
          ? { ...message, trace: appendTraceEvent(message.trace, event), runId: event.run_id, lastSeq: event.seq }
          : message,
      ),
    }));
  };

  const pollRun = async (
    runId: string,
    conversationId: string,
    assistantId: string,
    startAfter = -1,
    signal?: AbortSignal,
  ) => {
    let after = startAfter;
    setRunning(true);
    while (true) {
      if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
      const payload = await api.getChatRunEvents(runId, after);
      for (const event of payload.events) {
        after = event.seq;
        applyRunEvent(conversationId, assistantId, event);
      }
      if (isTerminalChatRunStatus(payload.run.status)) {
        updateConversation(conversationId, (conversation) => ({
          ...conversation,
          sessionId: payload.run.session_id ?? conversation.sessionId,
          messages: conversation.messages.map((message) =>
            message.id === assistantId
              ? {
                  ...message,
                  running: false,
                  content:
                    payload.run.status === "failed"
                      ? message.content || `DAClaw web chat failed: ${payload.run.error || "Unknown error"}`
                      : message.content || payload.run.final_response || "",
                }
              : message,
          ),
        }));
        break;
      }
      await delay(payload.events.length > 0 ? 200 : 900, signal);
    }
  };

  const streamChatFallback = async (
    body: Parameters<typeof api.streamChat>[0],
    conversationId: string,
    assistantId: string,
    signal?: AbortSignal,
  ) => {
    const runId = `legacy-stream-${Date.now().toString(36)}`;
    await api.streamChat(
      body,
      (event: ChatStreamEvent) => {
        const seq = legacyStreamSeqRef.current++;
        applyRunEvent(conversationId, assistantId, { ...event, run_id: runId, seq } as ChatRunEvent);
      },
      signal,
    );
  };

  useEffect(() => {
    const controller = new AbortController();
    for (const conversation of conversations) {
      for (const message of conversation.messages) {
        if (message.role !== "assistant" || !message.running || !message.runId) continue;
        void pollRun(message.runId, conversation.id, message.id, message.lastSeq ?? -1, controller.signal).catch((error) => {
          if (controller.signal.aborted) return;
          const text = error instanceof Error ? error.message : String(error);
          updateConversation(conversation.id, (current) => ({
            ...current,
            messages: current.messages.map((item) =>
              item.id === message.id
                ? { ...item, running: false, content: item.content || `DAClaw web chat failed: ${text}` }
                : item,
            ),
          }));
        });
      }
    }
    return () => controller.abort();
    // Run only on page mount. Running messages are persisted with runId/lastSeq.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const submit = async () => {
    const text = normalizeMessageText(input.trim());
    if ((!text && pendingFiles.length === 0) || activeRunning || !active) return;

    const userMessage: ChatMessage = {
      id: makeId("msg"),
      role: "user",
      content: text || "Please analyze the uploaded file.",
      attachments: pendingFiles,
    };
    const assistantId = makeId("msg");
    const conversationId = active.id;
    const history: ChatStreamMessage[] = active.messages
      .filter((message) => message.content.trim())
      .map((message) => ({ role: message.role, content: message.content }));

    updateConversation(conversationId, (conversation) => ({
      ...conversation,
      title: conversation.messages.length === 0 ? titleFrom(text || pendingFiles[0]?.name || "") : conversation.title,
      updatedAt: Date.now(),
      messages: [
        ...conversation.messages,
        userMessage,
        { id: assistantId, role: "assistant", content: "", trace: [], running: true, lastSeq: -1 },
      ],
    }));
    setInput("");
    setPendingFiles([]);
    setRunning(true);

    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const body = {
        message: userMessage.content,
        history,
        attachments: userMessage.attachments ?? [],
        session_id: active.sessionId ?? null,
      };
      const run = await api.startChatRun(body);
      updateConversation(conversationId, (conversation) => ({
        ...conversation,
        messages: conversation.messages.map((message) =>
          message.id === assistantId ? { ...message, runId: run.run_id, lastSeq: -1 } : message,
        ),
      }));
      await pollRun(run.run_id, conversationId, assistantId, -1, controller.signal);
    } catch (error) {
      if (!controller.signal.aborted && isMethodNotAllowedError(error)) {
        try {
          await streamChatFallback(
            {
              message: userMessage.content,
              history,
              attachments: userMessage.attachments ?? [],
              session_id: active.sessionId ?? null,
            },
            conversationId,
            assistantId,
            controller.signal,
          );
          return;
        } catch (fallbackError) {
          error = fallbackError;
        }
      }
      if (!controller.signal.aborted) {
        const message = error instanceof Error ? error.message : String(error);
        updateConversation(conversationId, (conversation) => ({
          ...conversation,
          messages: conversation.messages.map((item) =>
            item.id === assistantId
              ? { ...item, content: `DAClaw web chat failed: ${message}`, running: false }
              : item,
          ),
        }));
      }
    } finally {
      if (deltaTimerRef.current !== null) {
        window.clearTimeout(deltaTimerRef.current);
        deltaTimerRef.current = null;
      }
      flushAssistantDelta(conversationId, assistantId);
      setRunning(false);
      abortRef.current = null;
    }
  };

  const stop = () => {
    abortRef.current?.abort();
    if (deltaTimerRef.current !== null) {
      window.clearTimeout(deltaTimerRef.current);
      deltaTimerRef.current = null;
    }
    deltaBufferRef.current = "";
    updateActive((conversation) => ({
      ...conversation,
      messages: conversation.messages.map((message) =>
        message.running ? { ...message, running: false, content: message.content || "Stopped" } : message,
      ),
    }));
    setRunning(false);
  };

  return (
    <div className="flex h-full min-h-0 w-full overflow-hidden bg-background">
      {sidebarOpen && (
        <button
          type="button"
          className="fixed inset-0 z-40 bg-background/70 backdrop-blur-sm md:hidden"
          aria-label="Close chat sidebar"
          onClick={() => setSidebarOpen(false)}
        />
      )}
      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-50 flex w-72 flex-col border-r border-border bg-secondary/70 p-2 transition-[transform,width] duration-200 md:static md:z-auto md:translate-x-0",
          sidebarOpen ? "translate-x-0" : "-translate-x-full",
          sidebarCollapsed ? "md:w-[4.25rem]" : "md:w-72",
        )}
      >
        <div className={cn("flex items-center gap-2", sidebarCollapsed && "md:justify-center")}>
          <Button
            variant="ghost"
            className={cn(
              "h-10 flex-1 justify-start rounded-xl bg-card px-3 shadow-sm",
              sidebarCollapsed && "md:w-10 md:flex-none md:justify-center md:px-0",
            )}
            onClick={addConversation}
            aria-label="New chat"
            title="New chat"
          >
            <Plus className="h-4 w-4" />
            <span className={cn(sidebarCollapsed && "md:hidden")}>New chat</span>
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="hidden shrink-0 rounded-xl md:inline-flex"
            onClick={() => setSidebarCollapsed((value) => !value)}
            aria-label={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
            title={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          >
            {sidebarCollapsed ? <PanelLeftOpen className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
          </Button>
          <Button variant="ghost" size="icon" className="md:hidden" onClick={() => setSidebarOpen(false)}>
            <X className="h-4 w-4" />
          </Button>
        </div>
        <div className={cn("mt-3 flex-1 overflow-y-auto", sidebarCollapsed ? "md:space-y-2" : "space-y-1")}>
          {conversations.map((conversation) => (
            <button
              key={conversation.id}
              type="button"
              className={cn(
                "group flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left text-sm text-foreground hover:bg-card",
                conversation.id === active.id && "bg-card shadow-sm",
                sidebarCollapsed && "md:h-10 md:justify-center md:px-0",
              )}
              onClick={() => {
                setActiveId(conversation.id);
                setSidebarOpen(false);
              }}
              title={conversation.title}
            >
              <span className={cn("min-w-0 flex-1 truncate", sidebarCollapsed && "md:hidden")}>{conversation.title}</span>
              <span
                className={cn(
                  "hidden h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-muted text-xs font-semibold text-muted-foreground",
                  sidebarCollapsed && "md:flex",
                )}
              >
                {(conversation.title || "C").slice(0, 1).toUpperCase()}
              </span>
              <Trash2
                className={cn("h-4 w-4 shrink-0 opacity-0 hover:text-destructive group-hover:opacity-100", sidebarCollapsed && "md:hidden")}
                onClick={(event) => {
                  event.stopPropagation();
                  deleteConversation(conversation.id);
                }}
              />
            </button>
          ))}
        </div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col bg-background">
        <header className="flex h-11 shrink-0 items-center gap-2 border-b border-border px-3">
          <Button variant="ghost" size="icon" className="md:hidden" onClick={() => setSidebarOpen(true)}>
            <Menu className="h-5 w-5" />
          </Button>
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-sm font-semibold text-muted-foreground">DAClaw</h1>
          </div>
          {activeRunning && (
            <Badge variant="secondary" className="gap-1 rounded-full">
              <Loader2 className="h-3 w-3 animate-spin" />
              Working
            </Badge>
          )}
        </header>

        <div className="flex-1 overflow-y-auto px-4 sm:px-6">
          {active.messages.length === 0 ? (
            <div className="mx-auto flex h-full max-w-3xl flex-col items-center justify-center px-4 pb-16 text-center">
              <h2 className="text-3xl font-semibold tracking-tight">What can DAClaw analyze for you?</h2>
            </div>
          ) : (
            <div className="mx-auto max-w-5xl">
              {active.messages.map((message) => (
                <MessageBlock key={message.id} message={message} />
              ))}
              <div ref={bottomRef} />
            </div>
          )}
        </div>

        <div className="shrink-0 border-t border-border bg-background/95 px-3 py-3 backdrop-blur">
          <div className="mx-auto max-w-3xl">
            {pendingFiles.length > 0 && (
              <div className="mb-2 flex flex-wrap gap-2">
                {pendingFiles.map((file) => (
                  <Badge key={file.id} variant="outline" className="gap-1 rounded-full">
                    <FileText className="h-3 w-3" />
                    <span className="max-w-44 truncate">{file.name}</span>
                    <span className="text-muted-foreground">{formatBytes(file.size)}</span>
                    <button type="button" onClick={() => removePendingFile(file.id)}>
                      <X className="h-3 w-3" />
                    </button>
                  </Badge>
                ))}
              </div>
            )}
            {uploadError && (
              <div className="mb-2 flex items-start gap-2 rounded-2xl border border-destructive/25 bg-destructive/5 px-3 py-2 text-sm text-destructive">
                <span className="min-w-0 flex-1">Upload failed: {uploadError}</span>
                <button type="button" onClick={() => setUploadError(null)} aria-label="Dismiss upload error">
                  <X className="h-4 w-4" />
                </button>
              </div>
            )}
            <div className="rounded-[24px] border border-border bg-card px-3 py-2 shadow-[0_2px_14px_rgba(15,23,42,0.08)]">
              <input
                ref={fileInputRef}
                type="file"
                multiple
                className="hidden"
                onChange={(event) => handleFiles(event.target.files)}
              />
              <div className="flex items-end gap-2">
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-9 w-9 shrink-0 rounded-full"
                  disabled={uploading || activeRunning}
                  onClick={() => fileInputRef.current?.click()}
                  aria-label="Upload files"
                >
                  {uploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Paperclip className="h-4 w-4" />}
                </Button>
                <textarea
                  value={input}
                  rows={1}
                  onChange={(event) => setInput(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      submit();
                    }
                  }}
                  placeholder="Ask DAClaw"
                  className="max-h-32 min-h-9 flex-1 resize-none bg-transparent py-2 text-[15px] leading-5 outline-none placeholder:text-muted-foreground"
                  disabled={activeRunning}
                />
                {activeRunning ? (
                  <Button variant="outline" className="h-9 shrink-0 rounded-full px-4" onClick={stop}>
                    Stop
                  </Button>
                ) : (
                  <Button
                    size="icon"
                    className="h-9 w-9 shrink-0 rounded-full"
                    onClick={submit}
                    disabled={uploading || (!input.trim() && pendingFiles.length === 0)}
                  >
                    <Send className="h-4 w-4" />
                  </Button>
                )}
              </div>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}

