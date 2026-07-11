import { NavLink, Outlet } from "react-router-dom";
import { Trophy, FlaskConical } from "lucide-react";
import { useI18n } from "@/i18n";

// Subsections of the "Data Agent Research" area. Add more entries here as the
// research surface grows (e.g. datasets, eval runs) — the leaderboard is one.
const RESEARCH_SUBSECTIONS = [
  { path: "/research/leaderboard", en: "Leaderboard", zh: "榜单", icon: Trophy },
] as const;

export default function ResearchPage() {
  const { locale } = useI18n();
  const zh = locale === "zh";
  return (
    <div className="mission-shell flex h-full flex-col bg-[#07111f] text-slate-100">
      <div className="flex items-center gap-3 border-b border-white/5 bg-[#081321]/88 px-5 py-3 backdrop-blur-xl">
        <FlaskConical className="h-5 w-5 text-[#2DD4BF]" />
        <span className="text-sm font-semibold">{zh ? "评测" : "Benchmark"}</span>
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
