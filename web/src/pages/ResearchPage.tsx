import { NavLink, Outlet } from "react-router-dom";
import { Trophy, FlaskConical, Wand2 } from "lucide-react";
import { useI18n } from "@/i18n";

// Subsections of the "Research" area: the benchmark leaderboard + data synthesis.
const RESEARCH_SUBSECTIONS = [
  { path: "/research/leaderboard", en: "Benchmark", zh: "评测", icon: Trophy },
  { path: "/research/synthesis", en: "Data Synthesis", zh: "数据合成", icon: Wand2 },
] as const;

export default function ResearchPage() {
  const { locale } = useI18n();
  const zh = locale === "zh";
  return (
    <div className="mission-shell flex h-full flex-col bg-[#1a2536] text-slate-100">
      <div className="flex items-center gap-3 border-b border-white/5 bg-[#1e2a3d]/88 px-5 py-3 backdrop-blur-xl">
        <FlaskConical className="h-5 w-5 text-[#2DD4BF]" />
        <span className="text-sm font-semibold">{zh ? "科研" : "Research"}</span>
        <div className="ml-4 flex items-center gap-1">
          {RESEARCH_SUBSECTIONS.map(({ path, en, zh: zhLabel, icon: Icon }) => (
            <NavLink
              key={path}
              to={path}
              className={({ isActive }) =>
                `inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm font-medium transition-colors ${
                  isActive
                    ? "bg-foreground text-background"
                    : "text-muted-foreground hover:bg-secondary hover:text-foreground"
                }`
              }
            >
              <Icon className="h-3.5 w-3.5" />
              {zh ? zhLabel : en}
            </NavLink>
          ))}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        <Outlet />
      </div>
    </div>
  );
}
