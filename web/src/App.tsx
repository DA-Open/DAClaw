import { useState } from "react";
import { Navigate, NavLink, Route, Routes } from "react-router-dom";
import {
  Activity,
  BarChart3,
  Boxes,
  ChevronDown,
  Clock,
  FileText,
  KeyRound,
  MessageSquare,
  Package,
  Settings,
  Sparkles,
  Trophy,
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
import DataAgentPage from "@/pages/DataAgentPage";
import LeaderboardPage from "@/pages/LeaderboardPage";
import ResearchPage from "@/pages/ResearchPage";
import EtlPage from "@/pages/EtlPage";
import { LanguageSwitcher } from "@/components/LanguageSwitcher";
import { useI18n } from "@/i18n";
import { useThemePreference } from "@/lib/theme";

// Two primary destinations shown in the top bar.
const PRIMARY_NAV = [
  { path: "/data-agent", key: "dataAgent", icon: Sparkles },
  { path: "/etl", key: "etl", icon: Boxes },
  { path: "/research", key: "research", icon: Trophy },
] as const;

// Everything else lives under the "菜单 / Menu" dropdown.
const MORE_NAV = [
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
      dataAgent: "Data Analysis",
      etl: "Data Engineering",
      research: "Benchmark",
      leaderboard: "Leaderboard",
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
    more: "More",
    assistant: "Assistant",
    newChat: "New chat",
    threadPrefix: "Thread",
    thread: "Data analysis workspace",
    local: "Local first",
  },
  zh: {
    nav: {
      workspace: "工作区",
      dataAgent: "数据分析",
      etl: "数据工程",
      research: "评测",
      leaderboard: "榜单",
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
    more: "更多",
    assistant: "助手",
    newChat: "新会话",
    threadPrefix: "主题",
    thread: "数据分析工作台",
    local: "本地优先",
  },
};

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
          {MORE_NAV.map(({ path, key, icon: Icon }) => (
            <NavLink
              key={path}
              to={path}
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
  const [menuOpen, setMenuOpen] = useState(false);

  return (
    <div className="mission-app min-h-screen overflow-x-hidden bg-[#07111f] text-slate-100">
      <header className="sticky top-0 z-30 border-b border-cyan-300/10 bg-[#07111f]/90 backdrop-blur-xl">
        <div className="flex h-12 items-center gap-3 px-3 sm:px-5">
          <button
            type="button"
            className="inline-flex h-9 shrink-0 items-center gap-2 rounded-xl border border-cyan-300/15 bg-white/[0.035] pl-1.5 pr-2.5 text-sm font-semibold tracking-tight shadow-[0_8px_30px_rgba(0,0,0,.18)] hover:border-cyan-300/30 hover:bg-cyan-300/5"
            onClick={() => setMenuOpen((value) => !value)}
            aria-label="DAClaw menu"
          >
            <span className="flex h-6 w-6 items-center justify-center rounded-lg bg-gradient-to-br from-blue-500 to-cyan-300 text-[11px] font-black text-[#041817] shadow-[0_0_20px_rgba(77,211,255,.25)]">
              DA
            </span>
            <span className="hidden sm:inline">DAClaw</span>
            <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
          </button>

          <div className="flex h-9 items-center overflow-hidden rounded-xl border border-white/7 bg-white/[0.025] p-0.5">
            {PRIMARY_NAV.map(({ path, key, icon: Icon }) => (
              <NavLink
                key={path}
                to={path}
                className={({ isActive }) =>
                  `inline-flex h-8 items-center gap-1.5 rounded-full px-3 text-sm font-medium transition-colors ${
                    isActive ? "bg-gradient-to-r from-blue-500/90 to-cyan-400/90 text-[#041817] shadow-[0_0_22px_rgba(63,174,255,.18)]" : "text-slate-500 hover:text-slate-100"
                  }`
                }
              >
                <Icon className="h-4 w-4" />
                <span className="hidden sm:inline">{labels.nav[key]}</span>
              </NavLink>
            ))}
          </div>

          <div className="min-w-0 flex-1" />

          <LanguageSwitcher />
        </div>
      </header>

      <AppMenu open={menuOpen} onClose={() => setMenuOpen(false)} labels={labels} />

      <main className="h-[calc(100vh-3rem)] w-full overflow-auto bg-[radial-gradient(circle_at_50%_-20%,rgba(53,119,255,.14),transparent_40%)]">
        <Routes>
          <Route path="/" element={<Navigate to="/data-agent" replace />} />
          <Route path="/data-agent" element={<DataAgentPage />} />
          <Route path="/etl" element={<EtlPage />} />
          <Route path="/research" element={<ResearchPage />}>
            <Route index element={<Navigate to="/research/leaderboard" replace />} />
            <Route path="leaderboard" element={<LeaderboardPage />} />
          </Route>
          {/* back-compat: old direct leaderboard link */}
          <Route path="/leaderboard" element={<Navigate to="/research/leaderboard" replace />} />
          <Route path="/status" element={<StatusPage />} />
          <Route path="/sessions" element={<SessionsPage />} />
          <Route path="/analytics" element={<AnalyticsPage />} />
          <Route path="/logs" element={<LogsPage />} />
          <Route path="/cron" element={<CronPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/config" element={<ConfigPage />} />
          <Route path="/env" element={<EnvPage />} />
          <Route path="*" element={<Navigate to="/data-agent" replace />} />
        </Routes>
      </main>
    </div>
  );
}
