const BASE = "";

// Ephemeral session token for protected endpoints.
// Injected into index.html by the server — never fetched via API.
declare global {
  interface Window {
    __DACLAW_SESSION_TOKEN__?: string;
  }
}
let _sessionToken: string | null = null;

function compactResponsePreview(text: string): string {
  const normalized = text.replace(/\s+/g, " ").trim();
  if (!normalized) return "";
  return normalized.length > 180 ? `${normalized.slice(0, 180)}...` : normalized;
}

function extractErrorDetail(payload: unknown): string | null {
  if (!payload || typeof payload !== "object") return null;
  const record = payload as Record<string, unknown>;
  const detail = record.detail ?? record.error ?? record.message;
  if (typeof detail === "string") return detail;
  if (detail !== undefined) return JSON.stringify(detail);
  return null;
}

async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  // Inject the session token into all /api/ requests.
  const headers = new Headers(init?.headers);
  const token = window.__DACLAW_SESSION_TOKEN__;
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const res = await fetch(`${BASE}${url}`, { ...init, headers });
  const contentType = res.headers.get("content-type") ?? "";
  const text = await res.text();
  const trimmed = text.trim();
  const looksLikeJSON = trimmed.startsWith("{") || trimmed.startsWith("[");

  const parsePayload = (): unknown => {
    try {
      return JSON.parse(text);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      throw new Error(`Invalid JSON from ${url}: ${detail}`);
    }
  };

  if (!res.ok) {
    if (looksLikeJSON) {
      const payload = parsePayload();
      throw new Error(`${res.status}: ${extractErrorDetail(payload) ?? JSON.stringify(payload)}`);
    }
    const preview = compactResponsePreview(text) || res.statusText;
    throw new Error(`${res.status}: ${preview}`);
  }

  if (!text) {
    return undefined as T;
  }

  if (!contentType.includes("application/json") && !looksLikeJSON) {
    const preview = compactResponsePreview(text);
    throw new Error(
      `Expected JSON from ${url}, received ${contentType || "non-JSON response"}${preview ? `: ${preview}` : ""}`,
    );
  }

  return parsePayload() as T;
}

async function getSessionToken(): Promise<string> {
  if (_sessionToken) return _sessionToken;
  const injected = window.__DACLAW_SESSION_TOKEN__;
  if (injected) {
    _sessionToken = injected;
    return _sessionToken;
  }
  throw new Error("Session token not available — page must be served by the DAClaw dashboard server");
}

function apiHeaders(init?: HeadersInit): Headers {
  const headers = new Headers(init);
  const token = window.__DACLAW_SESSION_TOKEN__;
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  return headers;
}

async function downloadApiFile(url: string, filename: string): Promise<void> {
  const res = await fetch(`${BASE}${url}`, { headers: apiHeaders() });
  if (!res.ok) throw new Error(`${res.status}: ${res.statusText}`);
  const blob = await res.blob();
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(objectUrl);
}

export const api = {
  downloadWorkspaceFile: (fileId: string, filename: string) =>
    downloadApiFile(`/api/workspace/files/${encodeURIComponent(fileId)}/download`, filename),
  getStatus: () => fetchJSON<StatusResponse>("/api/status"),
  getSessions: (limit = 20, offset = 0) =>
    fetchJSON<PaginatedSessions>(`/api/sessions?limit=${limit}&offset=${offset}`),
  getSessionMessages: (id: string) =>
    fetchJSON<SessionMessagesResponse>(`/api/sessions/${encodeURIComponent(id)}/messages`),
  deleteSession: (id: string) =>
    fetchJSON<{ ok: boolean }>(`/api/sessions/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  getLogs: (params: { file?: string; lines?: number; level?: string; component?: string }) => {
    const qs = new URLSearchParams();
    if (params.file) qs.set("file", params.file);
    if (params.lines) qs.set("lines", String(params.lines));
    if (params.level && params.level !== "ALL") qs.set("level", params.level);
    if (params.component && params.component !== "all") qs.set("component", params.component);
    return fetchJSON<LogsResponse>(`/api/logs?${qs.toString()}`);
  },
  getAnalytics: (days: number) =>
    fetchJSON<AnalyticsResponse>(`/api/analytics/usage?days=${days}`),
  getWorkspaceFiles: (folder = "") => {
    const qs = new URLSearchParams();
    if (folder) qs.set("folder", folder);
    const suffix = qs.toString();
    return fetchJSON<WorkspaceFilesResponse>(`/api/workspace/files${suffix ? `?${suffix}` : ""}`);
  },
  getWorkspaceRoot: () =>
    fetchJSON<WorkspaceRootResponse>("/api/workspace/root"),
  setWorkspaceRoot: (path: string) =>
    fetchJSON<WorkspaceRootResponse>("/api/workspace/root", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    }),
  browseWorkspaceDirectories: (path = "") => {
    const qs = new URLSearchParams();
    if (path) qs.set("path", path);
    const suffix = qs.toString();
    return fetchJSON<WorkspaceBrowseResponse>(`/api/workspace/directories${suffix ? `?${suffix}` : ""}`);
  },
  pickWorkspaceRoot: () =>
    fetchJSON<WorkspaceRootResponse>("/api/workspace/root/pick", {
      method: "POST",
    }),
  getWorkspaceFilePreview: (id: string) =>
    fetchJSON<WorkspaceFilePreviewResponse>(`/api/workspace/files/${encodeURIComponent(id)}/preview`),
  deleteWorkspaceFile: (id: string) =>
    fetchJSON<{ ok: boolean }>(`/api/workspace/files/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  getConfig: () => fetchJSON<Record<string, unknown>>("/api/config"),
  getDefaults: () => fetchJSON<Record<string, unknown>>("/api/config/defaults"),
  getSchema: () => fetchJSON<{ fields: Record<string, unknown>; category_order: string[] }>("/api/config/schema"),
  getModelInfo: () => fetchJSON<ModelInfoResponse>("/api/model/info"),
  saveConfig: (config: Record<string, unknown>) =>
    fetchJSON<{ ok: boolean }>("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config }),
    }),
  getConfigRaw: () => fetchJSON<{ yaml: string }>("/api/config/raw"),
  saveConfigRaw: (yaml_text: string) =>
    fetchJSON<{ ok: boolean }>("/api/config/raw", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ yaml_text }),
    }),
  getEnvVars: () => fetchJSON<Record<string, EnvVarInfo>>("/api/env"),
  setEnvVar: (key: string, value: string) =>
    fetchJSON<{ ok: boolean }>("/api/env", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, value }),
    }),
  deleteEnvVar: (key: string) =>
    fetchJSON<{ ok: boolean }>("/api/env", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    }),
  revealEnvVar: async (key: string) => {
    const token = await getSessionToken();
    return fetchJSON<{ key: string; value: string }>("/api/env/reveal", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ key }),
    });
  },

  // Cron jobs
  getCronJobs: () => fetchJSON<CronJob[]>("/api/cron/jobs"),
  createCronJob: (job: { prompt: string; schedule: string; name?: string; deliver?: string }) =>
    fetchJSON<CronJob>("/api/cron/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(job),
    }),
  pauseCronJob: (id: string) =>
    fetchJSON<{ ok: boolean }>(`/api/cron/jobs/${id}/pause`, { method: "POST" }),
  resumeCronJob: (id: string) =>
    fetchJSON<{ ok: boolean }>(`/api/cron/jobs/${id}/resume`, { method: "POST" }),
  triggerCronJob: (id: string) =>
    fetchJSON<{ ok: boolean }>(`/api/cron/jobs/${id}/trigger`, { method: "POST" }),
  deleteCronJob: (id: string) =>
    fetchJSON<{ ok: boolean }>(`/api/cron/jobs/${id}`, { method: "DELETE" }),

  // Skills & Toolsets
  getSkills: () => fetchJSON<SkillInfo[]>("/api/skills"),
  toggleSkill: (name: string, enabled: boolean) =>
    fetchJSON<{ ok: boolean }>("/api/skills/toggle", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, enabled }),
    }),
  getSkillContent: (name: string) =>
    fetchJSON<SkillContent>(`/api/skills/${encodeURIComponent(name)}/content`),
  getToolsets: () => fetchJSON<ToolsetInfo[]>("/api/tools/toolsets"),

  // Session search (FTS5)
  searchSessions: (q: string) =>
    fetchJSON<SessionSearchResponse>(`/api/sessions/search?q=${encodeURIComponent(q)}`),

  // Web chat
  uploadChatFile: async (file: File) => {
    const body = new FormData();
    body.set("file", file);
    const res = await fetch(`${BASE}/api/chat/upload`, {
      method: "POST",
      headers: apiHeaders(),
      body,
    });
    if (!res.ok) {
      const text = await res.text().catch(() => res.statusText);
      let detail = text;
      try {
        const parsed = JSON.parse(text) as { detail?: string };
        detail = parsed.detail || text;
      } catch {
        // Keep the original response body.
      }
      throw new Error(`${res.status}: ${detail}`);
    }
    return res.json() as Promise<ChatAttachment>;
  },
  streamChat: async (
    body: ChatStreamRequest,
    onEvent: (event: ChatStreamEvent) => void,
    signal?: AbortSignal,
  ) => {
    const res = await fetch(`${BASE}/api/chat/stream`, {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
      signal,
    });
    if (!res.ok || !res.body) {
      const text = await res.text().catch(() => res.statusText);
      throw new Error(`${res.status}: ${text}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        const data = frame
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart())
          .join("\n");
        if (data) onEvent(JSON.parse(data) as ChatStreamEvent);
      }
    }

    if (buffer.trim()) {
      const data = buffer
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n");
      if (data) onEvent(JSON.parse(data) as ChatStreamEvent);
    }
  },
  startChatRun: (body: ChatStreamRequest) =>
    fetchJSON<ChatRunResponse>("/api/chat/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  getChatRunEvents: (runId: string, after = -1) =>
    fetchJSON<ChatRunEventsResponse>(
      `/api/chat/runs/${encodeURIComponent(runId)}/events?after=${encodeURIComponent(String(after))}`,
    ),

  // OAuth provider management
  getOAuthProviders: () =>
    fetchJSON<OAuthProvidersResponse>("/api/providers/oauth"),
  disconnectOAuthProvider: async (providerId: string) => {
    const token = await getSessionToken();
    return fetchJSON<{ ok: boolean; provider: string }>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}`,
      {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      },
    );
  },
  startOAuthLogin: async (providerId: string) => {
    const token = await getSessionToken();
    return fetchJSON<OAuthStartResponse>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}/start`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: "{}",
      },
    );
  },
  submitOAuthCode: async (providerId: string, sessionId: string, code: string) => {
    const token = await getSessionToken();
    return fetchJSON<OAuthSubmitResponse>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}/submit`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ session_id: sessionId, code }),
      },
    );
  },
  pollOAuthSession: (providerId: string, sessionId: string) =>
    fetchJSON<OAuthPollResponse>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}/poll/${encodeURIComponent(sessionId)}`,
    ),
  cancelOAuthSession: async (sessionId: string) => {
    const token = await getSessionToken();
    return fetchJSON<{ ok: boolean }>(
      `/api/providers/oauth/sessions/${encodeURIComponent(sessionId)}`,
      {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      },
    );
  },

  // ---- DataAgent workbench (Part 1) ----
  daConnect: (source: string, name?: string) =>
    fetchJSON<DAConnectResponse>("/api/da/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source, name }),
    }),
  daDbTypes: () => fetchJSON<{ types: DADbType[] }>("/api/da/db-types"),
  daConnectDb: (params: Record<string, unknown>) =>
    fetchJSON<DAConnectResponse>("/api/da/connect-db", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    }),
  daTables: () => fetchJSON<{ tables: DATableInfo[] }>("/api/da/tables"),
  daKnowledge: () => fetchJSON<{ sources: DAKnowledgeSource[] }>("/api/da/knowledge"),
  daKnowledgeConnect: (source: string, name?: string) =>
    fetchJSON<DAKnowledgeConnectResponse>("/api/da/knowledge/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source, name }),
    }),
  daKnowledgeDelete: (sourceId: string) =>
    fetchJSON<{ ok: boolean }>(`/api/da/knowledge/${encodeURIComponent(sourceId)}`, { method: "DELETE" }),
  daKnowledgeSearch: (query: string, sourceIds?: string[]) =>
    fetchJSON<DAKnowledgeSearchResponse>("/api/da/knowledge/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, source_ids: sourceIds }),
    }),
  daWorkflowPlan: (goal: string, tables?: string[]) =>
    fetchJSON<DAWorkflowPlanResponse>("/api/da/workflow/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal, tables }),
    }),
  daWorkflows: () => fetchJSON<{ workflows: DAWorkflowSummary[] }>("/api/da/workflows"),
  daWorkflowCompile: (workflow: DAWorkflow) =>
    fetchJSON<DAWorkflowPlanResponse>("/api/da/workflow/compile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(workflow),
    }),
  daQuery: (sql: string) =>
    fetchJSON<DAQueryResponse>("/api/da/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sql }),
    }),
  daNl2sql: (request: string) =>
    fetchJSON<DANl2sqlResponse>("/api/da/nl2sql", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request }),
    }),
  daVisualize: (sql: string, title: string, chartType?: string) =>
    fetchJSON<DAVizResponse>("/api/da/visualize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sql, title, chart_type: chartType }),
    }),
  daDashboard: (table: string, title?: string) =>
    fetchJSON<DAVizResponse>("/api/da/dashboard", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ table, title }),
    }),
  daExcel: (payload: Record<string, unknown>) =>
    fetchJSON<DAExcelResponse>("/api/da/excel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  daChat: (messages: { role: string; content: string }[]) =>
    fetchJSON<DAChatResponse>("/api/da/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages }),
    }),
  // Scoped Data Agent chat (agentic, tool-calling). Events are read via the
  // shared getChatRunEvents endpoint.
  startDaAgentRun: (body: {
    message: string;
    history?: { role: string; content: string }[];
    session_id?: string | null;
    workflow?: DAWorkflow | null;
  }) =>
    fetchJSON<ChatRunResponse>("/api/da/agent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ history: [], ...body }),
    }),
  daTableSample: (table: string, limit = 25) =>
    fetchJSON<DATableSampleResponse>(
      `/api/da/table/${encodeURIComponent(table)}/sample?limit=${encodeURIComponent(String(limit))}`,
    ),
  daClarify: (runId: string, answer: string) =>
    fetchJSON<{ ok: boolean; error?: string }>(`/api/da/clarify/${encodeURIComponent(runId)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answer }),
    }),
  daSuggest: (limit = 4) =>
    fetchJSON<{ ok: boolean; suggestions: string[]; source?: string }>(`/api/da/suggest?limit=${limit}`),
  daSynthesize: (payload: Record<string, unknown>) =>
    fetchJSON<DASynthesizeResponse>("/api/da/synthesize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),

  // ---- ETL / auto-dbt ----
  dbtBuild: (goal: string) =>
    fetchJSON<DbtBuildResponse>("/api/da/dbt/build", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal }),
    }),
  dbtModel: (name: string, limit = 50) =>
    fetchJSON<DbtModelResponse>(
      `/api/da/dbt/model/${encodeURIComponent(name)}?limit=${encodeURIComponent(String(limit))}`,
    ),

  // ---- Leaderboard (Part 2) ----
  lbOverview: () => fetchJSON<LeaderboardOverview>("/api/lb/overview"),
  lbRaw: () => fetchJSON<LeaderboardRaw>("/api/lb/raw"),
  lbEvaluate: (payload: { model: string; harness: string; benchmark: string; split?: string }) =>
    fetchJSON<{ ok: boolean; entry?: LeaderboardEntry; note?: string; error?: string }>(
      "/api/lb/evaluate",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
};

export interface PlatformStatus {
  error_code?: string;
  error_message?: string;
  state: string;
  updated_at: string;
}

export interface StatusResponse {
  active_sessions: number;
  config_path: string;
  config_version: number;
  env_path: string;
  gateway_exit_reason: string | null;
  gateway_pid: number | null;
  gateway_platforms: Record<string, PlatformStatus>;
  gateway_running: boolean;
  gateway_state: string | null;
  gateway_updated_at: string | null;
  daclaw_home: string;
  latest_config_version: number;
  release_date: string;
  version: string;
}

export interface SessionInfo {
  id: string;
  source: string | null;
  model: string | null;
  title: string | null;
  started_at: number;
  ended_at: number | null;
  last_active: number;
  is_active: boolean;
  message_count: number;
  tool_call_count: number;
  input_tokens: number;
  output_tokens: number;
  preview: string | null;
}

export interface PaginatedSessions {
  sessions: SessionInfo[];
  total: number;
  limit: number;
  offset: number;
}

export interface EnvVarInfo {
  is_set: boolean;
  redacted_value: string | null;
  description: string;
  url: string | null;
  category: string;
  is_password: boolean;
  tools: string[];
  advanced: boolean;
}

export interface SessionMessage {
  role: "user" | "assistant" | "system" | "tool";
  content: string | null;
  tool_calls?: Array<{
    id: string;
    function: { name: string; arguments: string };
  }>;
  tool_name?: string;
  tool_call_id?: string;
  timestamp?: number;
}

export interface SessionMessagesResponse {
  session_id: string;
  messages: SessionMessage[];
}

export interface LogsResponse {
  file: string;
  lines: string[];
}

export interface AnalyticsDailyEntry {
  day: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  reasoning_tokens: number;
  estimated_cost: number;
  actual_cost: number;
  sessions: number;
}

export interface AnalyticsModelEntry {
  model: string;
  input_tokens: number;
  output_tokens: number;
  estimated_cost: number;
  sessions: number;
}

export interface AnalyticsResponse {
  daily: AnalyticsDailyEntry[];
  by_model: AnalyticsModelEntry[];
  totals: {
    total_input: number;
    total_output: number;
    total_cache_read: number;
    total_reasoning: number;
    total_estimated_cost: number;
    total_actual_cost: number;
    total_sessions: number;
  };
}

export interface WorkspaceFile {
  id: string;
  upload_id: string;
  name: string;
  relative_path: string;
  path: string;
  size: number;
  modified_at: number;
  created_at: number;
  suffix: string;
  kind: "dataset" | "database" | "report" | "dashboard" | "document" | "visual" | "artifact" | string;
  content_type: string;
  previewable: boolean;
}

export interface WorkspaceDirectory {
  name: string;
  relative_path: string;
  path: string;
  modified_at: number;
  file_count: number;
  directory_count: number;
}

export interface WorkspaceBrowseDirectory {
  name: string;
  path: string;
  modified_at: number;
  file_count: number;
  directory_count: number;
}

export interface WorkspaceBrowseRoot {
  name: string;
  path: string;
}

export interface WorkspaceBrowseResponse {
  path: string;
  parent: string | null;
  roots: WorkspaceBrowseRoot[];
  directories: WorkspaceBrowseDirectory[];
}

export interface WorkspaceFilesResponse {
  root: string;
  folder: string;
  current_path: string;
  parent: string | null;
  directories: WorkspaceDirectory[];
  files: WorkspaceFile[];
  summary: Record<string, number>;
  truncated: boolean;
  limit: number;
}

export interface WorkspaceRootResponse {
  root: string;
  default_root: string;
  terminal_cwd: string;
  exists: boolean;
  cancelled?: boolean;
}

export type WorkspacePreview =
  | { type: "table"; columns: string[]; rows: string[][]; truncated: boolean }
  | { type: "database"; tables: Array<{ name: string; type: string; columns: Array<{ name: string; type: string }>; row_count: number | null; sample: Record<string, unknown>[] }> }
  | { type: "html"; html: string; truncated: boolean }
  | { type: "json" | "text"; text: string; truncated: boolean; line_count: number }
  | { type: "unsupported"; message: string };

export interface WorkspaceFilePreviewResponse {
  file: WorkspaceFile;
  preview: WorkspacePreview;
}

export interface CronJob {
  id: string;
  name?: string;
  prompt: string;
  schedule: { kind: string; expr: string; display: string };
  schedule_display: string;
  enabled: boolean;
  state: string;
  deliver?: string;
  last_run_at?: string | null;
  next_run_at?: string | null;
  last_error?: string | null;
}

export interface SkillInfo {
  name: string;
  description: string;
  category: string;
  enabled: boolean;
}

export interface SkillContent {
  ok: boolean;
  name: string;
  description: string;
  content: string;
  tags: string[];
  path: string;
}

export interface ToolsetInfo {
  name: string;
  label: string;
  description: string;
  enabled: boolean;
  configured: boolean;
  tools: string[];
}

export interface SessionSearchResult {
  session_id: string;
  snippet: string;
  role: string | null;
  source: string | null;
  model: string | null;
  session_started: number | null;
}

export interface SessionSearchResponse {
  results: SessionSearchResult[];
}

export interface ChatAttachment {
  id: string;
  name: string;
  path: string;
  size: number;
  content_type: string;
}

export interface ChatStreamMessage {
  role: "user" | "assistant" | "system" | "tool";
  content: string;
}

export interface ChatStreamRequest {
  message: string;
  history: ChatStreamMessage[];
  attachments: ChatAttachment[];
  session_id?: string | null;
}

export interface DAArtifact {
  kind: "chart" | "dashboard" | "document" | "story" | "excel" | string;
  workspace_file: string;
  file_id: string;
  preview_url: string;
  download_url: string;
  artifact_id?: string | null;
  title?: string | null;
  row_count?: number | null;
}

export interface DASkillEvent {
  action: string;
  name: string;
  category: string;
  kept: boolean;
  quality_status: string;
  score?: number | null;
  summary?: string;
}
export interface ChatTraceEvent {
  type: string;
  ts?: number;
  event?: string;
  message?: string;
  text?: string;
  iteration?: number;
  previous_tools?: unknown[];
  name?: string;
  tool_id?: string;
  summary?: string;
  preview?: string;
  args?: Record<string, unknown>;
  result?: string;
  duration?: number | null;
  is_error?: boolean;
  artifact?: DAArtifact;
  skill?: DASkillEvent;
}

export type ChatStreamEvent =
  | ({ type: "assistant_delta"; delta: string; ts?: number; run_id?: string; seq?: number })
  | ({ type: "heartbeat"; message: string; elapsed: number; idle?: number; ts?: number; run_id?: string; seq?: number })
  | ({
      type: "done";
      final_response: string;
      session_id?: string | null;
      completed: boolean;
      api_calls: number;
      elapsed: number;
      trace: ChatTraceEvent[];
      ts?: number;
      run_id?: string;
      seq?: number;
    })
  | ({ type: "error"; message: string; elapsed?: number; ts?: number; run_id?: string; seq?: number })
  | ChatTraceEvent;

export interface ChatRunResponse {
  run_id: string;
  status: "queued" | "running" | "completed" | "failed" | string;
  created_at: number;
  updated_at: number;
  next_seq: number;
  session_id?: string | null;
  final_response?: string;
  error?: string | null;
}

export type ChatRunEvent = ChatStreamEvent & {
  run_id: string;
  seq: number;
};

export interface ChatRunEventsResponse {
  run: ChatRunResponse;
  events: ChatRunEvent[];
  next_seq: number;
}

// ── Model info types ──────────────────────────────────────────────────

export interface ModelInfoResponse {
  model: string;
  provider: string;
  auto_context_length: number;
  config_context_length: number;
  effective_context_length: number;
  capabilities: {
    supports_tools?: boolean;
    supports_vision?: boolean;
    supports_reasoning?: boolean;
    context_window?: number;
    max_output_tokens?: number;
    model_family?: string;
  };
}

// ── OAuth provider types ────────────────────────────────────────────────

export interface OAuthProviderStatus {
  logged_in: boolean;
  source?: string | null;
  source_label?: string | null;
  token_preview?: string | null;
  expires_at?: string | null;
  has_refresh_token?: boolean;
  last_refresh?: string | null;
  error?: string;
}

export interface OAuthProvider {
  id: string;
  name: string;
  /** "pkce" (browser redirect + paste code), "device_code" (show code + URL),
   *  or "external" (delegated to a separate CLI like Claude Code or Qwen). */
  flow: "pkce" | "device_code" | "external";
  cli_command: string;
  docs_url: string;
  status: OAuthProviderStatus;
}

export interface OAuthProvidersResponse {
  providers: OAuthProvider[];
}

/** Discriminated union — the shape of /start depends on the flow. */
export type OAuthStartResponse =
  | {
      session_id: string;
      flow: "pkce";
      auth_url: string;
      expires_in: number;
    }
  | {
      session_id: string;
      flow: "device_code";
      user_code: string;
      verification_url: string;
      expires_in: number;
      poll_interval: number;
    };

export interface OAuthSubmitResponse {
  ok: boolean;
  status: "approved" | "error";
  message?: string;
}

export interface OAuthPollResponse {
  session_id: string;
  status: "pending" | "approved" | "denied" | "expired" | "error";
  error_message?: string | null;
  expires_at?: number | null;
}

// ---- DataAgent workbench types (Part 1) ----
export interface DAColumn {
  name: string;
  type: string;
}

export interface DAWorkflowNode {
  id: string;
  kind: string;
  label: string;
  description: string;
  depends_on: string[];
  tool: string;
  output_type: string;
  status: "planned" | "running" | "completed" | "error" | string;
  meta?: Record<string, unknown>;
}
export interface DAWorkflow {
  workflow_id: string;
  goal: string;
  nodes: DAWorkflowNode[];
  edges: { from: string; to: string }[];
  execution_layers: string[][];
  selected_tables: string[];
  knowledge_sources: string[];
  validation: {
    ok: boolean;
    issues: { code: string; message: string }[];
    warnings: { code: string; message: string }[];
  };
  optimizer: {
    parallel_layers: number;
    max_parallelism: number;
    critical_path_nodes: number;
    total_nodes: number;
  };
}
export type DAWorkflowPlanResponse = DAWorkflow & { ok: boolean };
export interface DAWorkflowSummary {
  workflow_id: string;
  goal: string;
  status: string;
  saved_at: number;
  nodes: number;
  layers: number;
  validation_ok: boolean;
}
export interface DAKnowledgeSource {
  source_id: string;
  name: string;
  path: string;
  kind: string;
  size: number;
  characters: number;
  updated_at: number;
}
export interface DAKnowledgeConnectResponse extends DAKnowledgeSource {
  ok: boolean;
  chunk_count?: number;
  error?: string;
}
export interface DAKnowledgeSearchResponse {
  ok: boolean;
  query: string;
  sources: number;
  results: { source_id: string; source: string; kind: string; chunk: number; score: number; text: string }[];
}

// ---- ETL / auto-dbt types ----
export interface DbtModel {
  name: string;
  layer: "staging" | "intermediate" | "marts" | string;
  materialized?: string;
  description?: string;
  sql?: string;
  compiled_sql?: string;
  order?: number;
  status: "ok" | "error" | "skipped" | string;
  row_count?: number;
  columns?: string[];
  preview?: unknown[][];
  error?: string;
}
export interface DbtDagNode {
  id: string;
  kind: "model" | "source" | string;
  layer: string;
  label: string;
  description?: string;
  materialized?: string;
}
export interface DbtBuildResponse {
  ok: boolean;
  project?: string;
  goal?: string;
  explanation?: string;
  models?: DbtModel[];
  dag?: { nodes: DbtDagNode[]; edges: { from: string; to: string }[] };
  layers?: string[];
  final?: string | null;
  final_result?: { table: string; columns: string[]; rows: unknown[][]; row_count: number } | null;
  n_models?: number;
  n_ok?: number;
  export?: { root: string; n_files: number };
  error?: string;
}
export interface DbtModelResponse {
  ok: boolean;
  table?: string;
  columns?: string[];
  rows?: unknown[][];
  row_count?: number;
  error?: string;
}


export interface DATableInfo {
  table: string;
  schema: DAColumn[];
  row_count?: number | null;
}
export interface DASynthFile {
  format: string;
  file: string;
  n_rows: number;
  download_url?: string;
}
export interface DASynthesizeResponse {
  ok: boolean;
  mode?: string;
  style?: string;
  source?: string;
  columns?: string[];
  n_rows?: number;
  preview?: Record<string, unknown>[];
  files?: DASynthFile[];
  error?: string;
}
export interface DATableSampleResponse {
  ok: boolean;
  table?: string;
  row_count?: number | null;
  columns?: string[] | null;
  sample_rows?: Record<string, unknown>[] | null;
  column_stats?: Record<string, unknown> | null;
  error?: string;
}
export interface DAConnectResponse {
  ok: boolean;
  connected?: string;
  kind?: string;
  dbtype?: string;
  via?: string;
  tables?: DATableInfo[];
  all_tables?: string[];
  error?: string;
}
export interface DADbType {
  id: string;
  label: string;
  fields: string[];
  default_port?: number;
  hint?: string;
}
export interface DAQueryResponse {
  ok: boolean;
  columns?: DAColumn[];
  row_count?: number;
  sample_rows?: unknown[][];
  error?: string;
}
export interface DAPipelineStage {
  name: string;
  purpose: string;
  sql: string;
  status: string;
  row_count?: number;
  columns?: string[];
  preview?: unknown[][];
  error?: string;
  order: number;
}
export interface DADagNode {
  id: string;
  kind: string;
  label: string;
  purpose?: string;
}
export interface DANl2sqlResponse {
  ok: boolean;
  request?: string;
  explanation?: string;
  stages?: DAPipelineStage[];
  dag?: { nodes: DADagNode[]; edges: { from: string; to: string }[] };
  final?: string;
  final_result?: { table: string; columns: string[]; rows: unknown[][]; row_count: number };
  n_stages?: number;
  n_ok?: number;
  error?: string;
}
export interface DAVizResponse {
  ok: boolean;
  html?: string;
  chart_type?: string;
  charts?: number;
  row_count?: number;
  check?: { ok: boolean; issues: string[] };
  error?: string;
}
export interface DAExcelResponse {
  ok: boolean;
  file?: string;
  path?: string;
  download_url?: string;
  sheets?: number;
  charts?: number;
  error?: string;
}
export interface DAChatResponse {
  ok: boolean;
  reply?: string;
  schema_tables?: string[];
  error?: string;
}

// ---- Leaderboard types (Part 2) ----
export interface LeaderboardHarness {
  id: string;
  name: string;
  vendor: string;
}
export interface LeaderboardBenchmark {
  id: string;
  name: string;
  paper: string;
  splits: string[];
  metric: string;
  sota_ceiling: number;
  level?: string;
}
export interface LeaderboardLevel {
  id: string;
  name: string;
  desc: string;
  color: string;
}
export interface LeaderboardEntry {
  model: string;
  harness: string;
  benchmark: string;
  split: string;
  score: number;
  metric: string;
  date: string;
  source: string;
}
export interface LeaderboardModel {
  id: string;
  name: string;
  creator: string;
  license: "open" | "proprietary";
  index: number;
}
export interface ModelRankRow {
  id: string;
  name: string;
  creator: string;
  license: "open" | "proprietary";
  index: number;
  agentic_score: number;
  best_harness: string;
}
export interface LeaderboardRaw {
  version: number;
  harnesses: LeaderboardHarness[];
  benchmarks: LeaderboardBenchmark[];
  levels?: LeaderboardLevel[];
  models: LeaderboardModel[];
  entries: LeaderboardEntry[];
}
export interface HarnessRankRow {
  harness: string;
  name: string;
  vendor: string;
  overall: number;
}
export interface BestCombo {
  model: string;
  harness: string;
  mean_score: number;
  n: number;
}
export interface LeaderboardOverview {
  matrix: {
    harnesses: LeaderboardHarness[];
    benchmarks: LeaderboardBenchmark[];
    levels?: LeaderboardLevel[];
    matrix: Record<string, Record<string, number>>;
    ranking: HarnessRankRow[];
  };
  best_combos: BestCombo[];
  model_ranking: ModelRankRow[];
}
