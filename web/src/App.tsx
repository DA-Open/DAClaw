import { useMemo, useState } from "react";
import { Navigate, NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import {
  Activity,
  BarChart3,
  Bot,
  ChevronDown,
  Clock,
  Database,
  FileText,
  KeyRound,
  Menu,
  MessageSquare,
  Package,
  Settings,
  X,
} from "lucide-react";
import StatusPage from "@/pages/StatusPage";
import ConfigPage from "@/pages/ConfigPage";
import EnvPage from "@/pages/EnvPage";
import SessionsPage from "@/pages/SessionsPage";
import LogsPage from "@/pages/LogsPage";
import AnalyticsPage from "@/pages/AnalyticsPage";
import CronPage from "@/pages/CronPage";
import SkillsPage from "@/pages/SkillsPage";
import ChatPage from "@/pages/ChatPage";
import WorkspacePage from "@/pages/WorkspacePage";
import { LanguageSwitcher } from "@/components/LanguageSwitcher";
import { useI18n } from "@/i18n";
import { useThemePreference } from "@/lib/theme";

const NAV_ITEMS = [
  { path: "/workspace", key: "workspace", icon: Database },
  { path: "/sessions", key: "sessions", icon: MessageSquare },
  { path: "/analytics", key: "usage", icon: BarChart3 },
  { path: "/skills", key: "skills", icon: Package },
  { path: "/status", key: "status", icon: Activity },
  { path: "/logs", key: "logs", icon: FileText },
  { path: "/cron", key: "cron", icon: Clock },
  { path: "/config", key: "config", icon: Settings },
  { path: "/env", key: "keys", icon: KeyRound },
] as const;

const SHELL_LABELS = {
  en: {
    nav: {
      workspace: "Workspace",
      analyze: "Chat",
      sessions: "Sessions",
      usage: "Usage",
      skills: "Skills",
      status: "Status",
      logs: "Logs",
      cron: "Cron",
      config: "Config",
      keys: "Keys",
    },
    menu: "Menu",
    assistant: "Assistant",
    newChat: "New chat",
    threadPrefix: "Thread",
    thread: "Data analysis workspace",
    local: "Local first",
  },
  zh: {
    nav: {
      workspace: "工作区",
      analyze: "对话",
      sessions: "会话",
      usage: "用量",
      skills: "技能",
      status: "状态",
      logs: "日志",
      cron: "定时任务",
      config: "配置",
      keys: "密钥",
    },
    menu: "菜单",
    assistant: "助手",
    newChat: "新会话",
    threadPrefix: "主题",
    thread: "数据分析工作台",
    local: "本地优先",
  },
};

function useActiveTitle(labels: (typeof SHELL_LABELS)["en"]) {
  const location = useLocation();
  return useMemo(() => {
    const active = NAV_ITEMS.find((item) => location.pathname === item.path || location.pathname.startsWith(`${item.path}/`));
    return active ? labels.nav[active.key] : labels.nav.workspace;
  }, [labels, location.pathname]);
}

function AppMenu({
  open,
  onClose,
  labels,
}: {
  open: boolean;
  onClose: () => void;
  labels: (typeof SHELL_LABELS)["en"];
}) {
  if (!open) return null;
  return (
    <>
      <button className="fixed inset-0 z-40 cursor-default bg-transparent" type="button" aria-label="Close menu" onClick={onClose} />
      <div className="fixed left-4 top-16 z-50 w-[19rem] border border-border bg-card shadow-xl">
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div className="flex items-center gap-3">
            <div className="flex h-8 w-8 items-center justify-center bg-foreground text-xs font-semibold text-background">DA</div>
            <div>
              <div className="text-sm font-semibold">DAClaw</div>
              <div className="text-xs text-muted-foreground">{labels.thread}</div>
            </div>
          </div>
          <button type="button" className="p-1 text-muted-foreground hover:text-foreground" onClick={onClose} aria-label="Close menu">
            <X className="h-4 w-4" />
          </button>
        </div>
        <nav className="grid p-2">
          {NAV_ITEMS.map(({ path, key, icon: Icon }) => (
            <NavLink
              key={path}
              to={path}
              end={path === "/workspace"}
              onClick={onClose}
              className={({ isActive }) =>
                `flex items-center gap-3 px-3 py-2.5 text-sm font-medium transition-colors ${
                  isActive ? "bg-foreground text-background" : "text-muted-foreground hover:bg-secondary hover:text-foreground"
                }`
              }
            >
              <Icon className="h-4 w-4" />
              {labels.nav[key]}
            </NavLink>
          ))}
        </nav>
      </div>
    </>
  );
}

export default function App() {
  const { locale } = useI18n();
  useThemePreference();
  const labels = SHELL_LABELS[locale];
  const title = useActiveTitle(labels);
  const [menuOpen, setMenuOpen] = useState(false);
  const navigate = useNavigate();

  const openWorkspaceAssistant = () => {
    navigate(`/workspace?assistant=1&t=${Date.now()}`);
  };

  return (
    <div className="min-h-screen overflow-x-hidden bg-background text-foreground">
      <header className="sticky top-0 z-30 border-b border-border bg-card/95 backdrop-blur">
        <div className="flex h-12 items-center gap-3 px-3 sm:px-5">
          <NavLink
            to="/workspace"
            className="inline-flex h-9 shrink-0 items-center gap-2 rounded-full px-1.5 pr-3 text-sm font-semibold tracking-tight hover:bg-secondary"
            aria-label="DAClaw"
          >
            <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-foreground text-[11px] font-bold text-background">
              DA
            </span>
            <span className="hidden sm:inline">DAClaw</span>
          </NavLink>

          <button
            type="button"
            className="inline-flex h-9 items-center gap-2 rounded-full border border-border bg-background px-3 text-sm font-medium hover:bg-secondary"
            onClick={() => setMenuOpen((value) => !value)}
          >
            <Menu className="h-4 w-4" />
            {labels.menu}
            <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
          </button>

          <div className="flex h-9 items-center overflow-hidden rounded-full border border-border bg-background p-0.5">
            <NavLink
              to="/workspace"
              end
              className={({ isActive }) =>
                `inline-flex h-8 items-center gap-1.5 rounded-full px-3 text-sm font-medium transition-colors ${
                  isActive ? "bg-foreground text-background" : "text-muted-foreground hover:text-foreground"
                }`
              }
            >
              <Database className="h-4 w-4" />
              <span className="hidden sm:inline">{labels.nav.workspace}</span>
            </NavLink>
            <NavLink
              to="/chat"
              className={({ isActive }) =>
                `inline-flex h-8 items-center gap-1.5 rounded-full px-3 text-sm font-medium transition-colors ${
                  isActive ? "bg-foreground text-background" : "text-muted-foreground hover:text-foreground"
                }`
              }
            >
              <Bot className="h-4 w-4" />
              <span className="hidden sm:inline">{labels.nav.analyze}</span>
            </NavLink>
          </div>

          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 items-center gap-2">
              <span className="truncate text-sm font-semibold">{title}</span>
              <span className="hidden truncate text-xs text-muted-foreground sm:inline">
                {labels.threadPrefix}: {labels.thread}
              </span>
            </div>
          </div>

          <div className="hidden items-center gap-3 text-xs text-muted-foreground md:flex">
            <span>{labels.local}</span>
            <span className="h-2 w-2 bg-emerald-500" />
          </div>
          <LanguageSwitcher />
          <button
            type="button"
            onClick={openWorkspaceAssistant}
            className="inline-flex h-9 items-center gap-2 rounded-full bg-foreground px-3 text-sm font-medium text-background hover:bg-foreground/90 sm:px-4"
            title={labels.assistant}
            aria-label={labels.assistant}
          >
            <Bot className="h-4 w-4" />
            <span className="hidden sm:inline">{labels.assistant}</span>
          </button>
        </div>
      </header>

      <AppMenu open={menuOpen} onClose={() => setMenuOpen(false)} labels={labels} />

      <main className="h-[calc(100vh-3rem)] w-full overflow-auto">
        <Routes>
          <Route path="/" element={<Navigate to="/workspace" replace />} />
          <Route path="/workspace" element={<WorkspacePage />} />
          <Route path="/status" element={<StatusPage />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/sessions" element={<SessionsPage />} />
          <Route path="/analytics" element={<AnalyticsPage />} />
          <Route path="/logs" element={<LogsPage />} />
          <Route path="/cron" element={<CronPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/config" element={<ConfigPage />} />
          <Route path="/env" element={<EnvPage />} />
          <Route path="*" element={<Navigate to="/workspace" replace />} />
        </Routes>
      </main>
    </div>
  );
}
