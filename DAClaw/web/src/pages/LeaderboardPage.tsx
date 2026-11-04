import { useEffect, useMemo, useState } from "react";
import {
  Trophy, Cpu, FlaskConical, RefreshCw, TrendingUp, Play, Loader2,
  Crown, Layers, Boxes, Database, Sparkles, BarChart3, Zap, Brain,
  Unlock, Lock,
} from "lucide-react";
import { api } from "@/lib/api";
import type {
  LeaderboardOverview, LeaderboardRaw, LeaderboardLevel, ModelRankRow,
} from "@/lib/api";
import { BrandIcon } from "@/components/BrandIcon";
import { useI18n } from "@/i18n";

// Distinct accent per harness — used for bars, dots, avatars everywhere.
const HARNESS_COLORS: Record<string, string> = {
  daclaw: "#2DD4BF",
  "claude-code": "#D97757",
  codex: "#10A37F",
  cursor: "#6366F1",
  cline: "#8B5CF6",
  opencode: "#0EA5E9",
  kilocode: "#F43F5E",
  openclaw: "#EC4899",
  hermes: "#F59E0B",
};

// Creator brand icons come from BrandIcon; harness accents from HARNESS_COLORS.

const LEVEL_FALLBACK: LeaderboardLevel[] = [
  { id: "L1", name: "L1 · Atomic", desc: "", color: "#2DD4BF" },
  { id: "L2", name: "L2 · Engineering", desc: "", color: "#6366F1" },
  { id: "L3", name: "L3 · Analysis", desc: "", color: "#F59E0B" },
  { id: "VIZ", name: "Text-to-Viz", desc: "", color: "#EC4899" },
];
const LEVEL_ICON: Record<string, typeof Database> = {
  L1: Database, L2: Boxes, L3: Sparkles, VIZ: BarChart3,
};

function ratioColor(score: number, ceiling: number): string {
  const r = ceiling ? score / ceiling : 0;
  if (r >= 0.9) return "text-emerald-500";
  if (r >= 0.7) return "text-teal-500";
  if (r >= 0.5) return "text-amber-500";
  return "text-muted-foreground";
}
function heatBg(score: number | null | undefined, ceiling: number): string {
  if (score == null) return "transparent";
  const r = Math.max(0, Math.min(1, ceiling ? score / ceiling : 0));
  const alpha = 0.08 + r * 0.5;
  return `rgba(45, 212, 191, ${alpha.toFixed(3)})`;
}
// intelligence-index bar color: green→teal→amber ramp
function indexColor(idx: number): string {
  if (idx >= 68) return "#10b981";
  if (idx >= 58) return "#2DD4BF";
  if (idx >= 45) return "#6366F1";
  if (idx >= 35) return "#F59E0B";
  return "#94a3b8";
}

type Tab = "models" | "ranking" | "matrix" | "combos";

export default function LeaderboardPage() {
  const { t } = useI18n();
  const T = t.lb;
  const [overview, setOverview] = useState<LeaderboardOverview | null>(null);
  const [raw, setRaw] = useState<LeaderboardRaw | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("models");
  const [licenseFilter, setLicenseFilter] = useState<"all" | "open" | "proprietary">("all");

  const [evalModel, setEvalModel] = useState("");
  const [evalHarness, setEvalHarness] = useState("");
  const [evalBenchmark, setEvalBenchmark] = useState("");
  const [evaluating, setEvaluating] = useState(false);
  const [evalMsg, setEvalMsg] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    Promise.all([api.lbOverview(), api.lbRaw()])
      .then(([o, r]) => { setOverview(o); setRaw(r); setError(null); })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  };
  useEffect(load, []);

  useEffect(() => {
    if (raw && !evalModel) {
      setEvalModel(raw.models[0]?.id ?? "");
      setEvalHarness(raw.harnesses[0]?.id ?? "");
      setEvalBenchmark(raw.benchmarks[0]?.id ?? "");
    }
  }, [raw, evalModel]);

  const runEvaluate = async () => {
    if (!evalModel || !evalHarness || !evalBenchmark) return;
    setEvaluating(true);
    setEvalMsg(null);
    try {
      const r = await api.lbEvaluate({ model: evalModel, harness: evalHarness, benchmark: evalBenchmark });
      if (r.ok && r.entry) {
        setEvalMsg(`✓ ${evalModel} × ${evalHarness}: ${r.entry.score}%`);
        load();
      } else setEvalMsg(`Error: ${r.error ?? "failed"}`);
    } catch (e) {
      setEvalMsg(String(e));
    } finally {
      setEvaluating(false);
    }
  };

  const maxOverall = useMemo(
    () => Math.max(1, ...(overview?.matrix.ranking.map((r) => r.overall) ?? [1])),
    [overview],
  );

  const levels = overview?.matrix.levels ?? LEVEL_FALLBACK;
  const benchesByLevel = useMemo(() => {
    const by: Record<string, typeof benchmarks> = {};
    for (const b of overview?.matrix.benchmarks ?? []) {
      const lvl = b.level ?? "L1";
      (by[lvl] ??= []).push(b);
    }
    return by;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [overview]);

  const modelRows: ModelRankRow[] = useMemo(() => {
    const rows = overview?.model_ranking ?? [];
    return licenseFilter === "all" ? rows : rows.filter((r) => r.license === licenseFilter);
  }, [overview, licenseFilter]);

  const nOpen = overview?.model_ranking.filter((r) => r.license === "open").length ?? 0;
  const nProp = (overview?.model_ranking.length ?? 0) - nOpen;
  const nCreators = new Set(overview?.model_ranking.map((r) => r.creator)).size;
  const maxIndex = Math.max(1, ...(overview?.model_ranking.map((r) => r.index) ?? [1]));

  if (loading && !overview) {
    return (
      <div className="flex h-full items-center justify-center text-muted-foreground">
        <RefreshCw className="mr-2 h-4 w-4 animate-spin" /> {T.loading}
      </div>
    );
  }
  if (error) return <div className="p-8 text-red-500">{T.failed}: {error}</div>;
  if (!overview || !raw) return null;

  const { ranking, benchmarks, harnesses, matrix } = overview.matrix;
  const top3 = ranking.slice(0, 3);

  return (
    <div className="mission-shell relative min-h-full overflow-hidden bg-[#07111f] text-slate-100">
      {/* ambient glow background */}
      <div className="pointer-events-none absolute inset-0 -z-10">
        <div className="absolute -left-40 -top-40 h-96 w-96 rounded-full bg-[#2DD4BF]/20 blur-[120px]" />
        <div className="absolute right-0 top-20 h-80 w-80 rounded-full bg-[#6366F1]/15 blur-[120px]" />
        <div className="absolute bottom-0 left-1/3 h-72 w-72 rounded-full bg-[#EC4899]/10 blur-[120px]" />
      </div>

      <div className="mx-auto max-w-6xl px-6 py-8">
        {/* ---- Hero ---- */}
        <div className="mb-8 flex items-end justify-between gap-4">
          <div>
            <div className="mb-2 inline-flex items-center gap-1.5 rounded-full border border-[#2DD4BF]/30 bg-[#2DD4BF]/10 px-3 py-1 text-xs font-medium text-[#2DD4BF]">
              <Zap className="h-3 w-3" /> Full-Stack Data-Agent Evaluation
            </div>
            <h1 className="flex items-center gap-3 bg-gradient-to-r from-[#2DD4BF] via-[#6366F1] to-[#EC4899] bg-clip-text text-3xl font-black tracking-tight text-transparent">
              <Trophy className="h-8 w-8 text-[#2DD4BF]" />
              {T.title}
            </h1>
            <p className="mt-2 max-w-2xl text-sm text-muted-foreground">{T.subtitle}</p>
          </div>
          <button
            onClick={load}
            className="inline-flex items-center gap-2 rounded-xl border border-border bg-card/70 px-3 py-2 text-sm backdrop-blur hover:bg-secondary"
          >
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} /> {T.refresh}
          </button>
        </div>

        {/* ---- Stat strip ---- */}
        <div className="mb-8 grid grid-cols-2 gap-3 md:grid-cols-4">
          {[
            { icon: Brain, label: T.models, value: overview.model_ranking.length, color: "#2DD4BF" },
            { icon: Layers, label: T.creators, value: nCreators, color: "#6366F1" },
            { icon: Cpu, label: T.harnesses, value: harnesses.length, color: "#F59E0B" },
            { icon: FlaskConical, label: T.benchmarks, value: benchmarks.length, color: "#EC4899" },
          ].map((s) => (
            <div key={s.label} className="relative overflow-hidden rounded-2xl border border-border bg-card/70 p-4 backdrop-blur">
              <div className="absolute -right-4 -top-4 h-16 w-16 rounded-full opacity-10" style={{ background: s.color }} />
              <s.icon className="h-5 w-5" style={{ color: s.color }} />
              <div className="mt-2 text-3xl font-black tabular-nums">{s.value}</div>
              <div className="text-xs text-muted-foreground">{s.label}</div>
            </div>
          ))}
        </div>

        {/* ---- Tabs ---- */}
        <div className="mb-5 flex flex-wrap gap-1 border-b border-border">
          {([
            ["models", T.tabModels, Brain],
            ["ranking", T.tabRanking, Trophy],
            ["matrix", T.tabMatrix, FlaskConical],
            ["combos", T.tabCombos, TrendingUp],
          ] as const).map(([id, label, Icon]) => (
            <button
              key={id}
              onClick={() => setTab(id)}
              className={`inline-flex items-center gap-2 border-b-2 px-4 py-2.5 text-sm font-medium transition-colors ${
                tab === id ? "border-[#2DD4BF] text-foreground" : "border-transparent text-muted-foreground hover:text-foreground"
              }`}
            >
              <Icon className="h-4 w-4" /> {label}
            </button>
          ))}
        </div>

        {/* ================= MODELS TAB (artificialanalysis style) ============ */}
        {tab === "models" && (
          <>
            {/* open vs proprietary filter + counts */}
            <div className="mb-4 flex flex-wrap items-center gap-2">
              {([
                ["all", `${T.filterAll} · ${overview.model_ranking.length}`, null],
                ["open", `${T.openWeights} · ${nOpen}`, Unlock],
                ["proprietary", `${T.proprietary} · ${nProp}`, Lock],
              ] as const).map(([id, label, Icon]) => (
                <button
                  key={id}
                  onClick={() => setLicenseFilter(id)}
                  className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-medium transition-colors ${
                    licenseFilter === id
                      ? "border-[#2DD4BF] bg-[#2DD4BF]/10 text-[#2DD4BF]"
                      : "border-border text-muted-foreground hover:bg-secondary"
                  }`}
                >
                  {Icon && <Icon className="h-3 w-3" />} {label}
                </button>
              ))}
            </div>

            <div className="overflow-hidden rounded-2xl border border-border bg-card/70 backdrop-blur">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-xs uppercase tracking-wide text-muted-foreground">
                    <th className="w-12 p-3 text-center font-semibold">#</th>
                    <th className="p-3 text-left font-semibold">{T.modelCol}</th>
                    <th className="hidden p-3 text-left font-semibold sm:table-cell">{T.creator}</th>
                    <th className="p-3 text-center font-semibold">{T.license}</th>
                    <th className="p-3 text-left font-semibold">{T.intelligenceIndex}</th>
                    <th className="p-3 text-right font-semibold">{T.agenticScore}</th>
                  </tr>
                </thead>
                <tbody>
                  {modelRows.map((m, i) => {
                    const ic = indexColor(m.index);
                    return (
                      <tr key={m.id} className="border-b border-border last:border-0 transition-colors hover:bg-secondary/40">
                        <td className="p-3 text-center">
                          {i < 3 ? (
                            <Crown className="mx-auto h-4 w-4" style={{ color: ["#F4B400", "#9CA3AF", "#CD7F32"][i] }} fill={["#F4B400", "#9CA3AF", "#CD7F32"][i]} />
                          ) : (
                            <span className="text-xs font-bold tabular-nums text-muted-foreground">{i + 1}</span>
                          )}
                        </td>
                        <td className="p-3">
                          <div className="flex items-center gap-2.5">
                            <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-white ring-1 ring-border dark:bg-slate-900">
                              <BrandIcon creator={m.creator} className="h-5 w-5" />
                            </span>
                            <span className="font-semibold">{m.name}</span>
                          </div>
                        </td>
                        <td className="hidden p-3 text-muted-foreground sm:table-cell">{m.creator}</td>
                        <td className="p-3 text-center">
                          {m.license === "open" ? (
                            <span className="inline-flex items-center gap-1 rounded-full border border-emerald-300/60 bg-emerald-50 px-2 py-0.5 text-[10px] font-semibold text-emerald-600 dark:bg-emerald-950/40">
                              <Unlock className="h-2.5 w-2.5" /> {T.open}
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1 rounded-full border border-slate-300/60 bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-slate-500 dark:bg-slate-800/60">
                              <Lock className="h-2.5 w-2.5" /> {T.proprietary}
                            </span>
                          )}
                        </td>
                        <td className="p-3">
                          <div className="flex items-center gap-2">
                            <div className="h-2 w-28 overflow-hidden rounded-full bg-secondary md:w-40">
                              <div className="h-full rounded-full transition-all duration-700" style={{ width: `${(m.index / maxIndex) * 100}%`, background: ic, boxShadow: `0 0 8px ${ic}66` }} />
                            </div>
                            <span className="w-7 text-xs font-black tabular-nums" style={{ color: ic }}>{m.index}</span>
                          </div>
                        </td>
                        <td className="p-3 text-right">
                          <span className="text-sm font-bold tabular-nums">{m.agentic_score}%</span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="pt-2 text-xs text-muted-foreground">{T.modelNote}</p>
          </>
        )}

        {/* ================= HARNESS RANKING TAB ============ */}
        {tab === "ranking" && (
          <>
            {/* Podium (top 3) */}
            <div className="mb-8 grid grid-cols-3 gap-3">
              {[1, 0, 2].map((idx) => {
                const r = top3[idx];
                if (!r) return <div key={idx} />;
                const rank = idx + 1;
                const c = HARNESS_COLORS[r.harness] ?? "#64748b";
                const podiumH = idx === 0 ? "h-36" : idx === 1 ? "h-28" : "h-24";
                return (
                  <div key={r.harness} className="flex flex-col items-center justify-end">
                    <div className="mb-2 flex h-12 w-12 items-center justify-center rounded-2xl text-sm font-black text-white shadow-lg" style={{ background: c, boxShadow: `0 8px 24px ${c}55` }}>
                      {r.name.slice(0, 2)}
                    </div>
                    <div className="text-center text-sm font-bold">{r.name}</div>
                    <div className="mb-2 text-xs text-muted-foreground">{r.vendor}</div>
                    <div className={`relative flex w-full ${podiumH} items-start justify-center rounded-t-xl border border-b-0 pt-3`} style={{ background: `linear-gradient(180deg, ${c}22, ${c}05)`, borderColor: `${c}44` }}>
                      {idx === 0 && <Crown className="absolute -top-3 h-6 w-6" style={{ color: c }} fill={c} />}
                      <div className="text-center">
                        <div className="text-2xl font-black tabular-nums" style={{ color: c }}>{r.overall}%</div>
                        <div className="mt-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">#{rank}</div>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
            <div className="space-y-2">
              {ranking.map((r, i) => {
                const c = HARNESS_COLORS[r.harness] ?? "#64748b";
                return (
                  <div key={r.harness} className="flex items-center gap-4 rounded-xl border border-border bg-card/70 p-3.5 backdrop-blur transition-all hover:shadow-md">
                    <div className="w-7 text-center text-sm font-black tabular-nums text-muted-foreground">
                      {i < 3 ? <Crown className="mx-auto h-5 w-5" style={{ color: c }} fill={c} /> : i + 1}
                    </div>
                    <div className="flex h-10 w-10 items-center justify-center rounded-xl text-xs font-black text-white" style={{ background: c, boxShadow: `0 4px 14px ${c}44` }}>
                      {r.name.slice(0, 2)}
                    </div>
                    <div className="w-36">
                      <div className="font-semibold">{r.name}</div>
                      <div className="text-xs text-muted-foreground">{r.vendor}</div>
                    </div>
                    <div className="flex-1">
                      <div className="h-3 w-full overflow-hidden rounded-full bg-secondary">
                        <div className="h-full rounded-full transition-all duration-700" style={{ width: `${(r.overall / maxOverall) * 100}%`, background: `linear-gradient(90deg, ${c}, ${c}CC)`, boxShadow: `0 0 12px ${c}66` }} />
                      </div>
                    </div>
                    <div className="w-16 text-right text-lg font-black tabular-nums" style={{ color: c }}>{r.overall}%</div>
                  </div>
                );
              })}
              <p className="pt-2 text-xs text-muted-foreground">{T.rankingNote}</p>
            </div>
          </>
        )}

        {/* ================= MATRIX TAB ============ */}
        {tab === "matrix" && (
          <>
            {/* Benchmark cards grouped by capability level */}
            <div className="mb-6 space-y-4">
              {levels.map((lvl) => {
                const list = benchesByLevel[lvl.id] ?? [];
                if (!list.length) return null;
                const Icon = LEVEL_ICON[lvl.id] ?? FlaskConical;
                return (
                  <div key={lvl.id}>
                    <div className="mb-2 flex items-center gap-2">
                      <span className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-bold text-white" style={{ background: lvl.color }}>
                        <Icon className="h-3.5 w-3.5" /> {lvl.name}
                      </span>
                      {lvl.desc && <span className="text-xs text-muted-foreground">{lvl.desc}</span>}
                    </div>
                    <div className="grid grid-cols-2 gap-2.5 md:grid-cols-3 lg:grid-cols-4">
                      {list.map((b) => (
                        <div key={b.id} className="group relative overflow-hidden rounded-xl border border-border bg-card/70 p-3 backdrop-blur transition-all hover:-translate-y-0.5 hover:shadow-lg" style={{ borderTopColor: lvl.color, borderTopWidth: 2 }}>
                          <div className="text-sm font-bold leading-tight">{b.name}</div>
                          <div className="mt-0.5 text-[10px] text-muted-foreground">{b.paper}</div>
                          <div className="mt-2 flex items-end justify-between">
                            <div className="text-[10px] text-muted-foreground">{b.metric}</div>
                            <div className="text-right">
                              <div className="text-[9px] uppercase tracking-wide text-muted-foreground">{T.sotaCeiling}</div>
                              <div className="text-sm font-black tabular-nums" style={{ color: lvl.color }}>{b.sota_ceiling}%</div>
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
            {/* Heatmap */}
            <div className="overflow-x-auto rounded-xl border border-border bg-card/70 backdrop-blur">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="border-b border-border">
                    <th className="sticky left-0 z-10 bg-card p-3 text-left font-semibold">{T.harnessCol}</th>
                    {benchmarks.map((b) => (
                      <th key={b.id} className="p-2 text-center align-bottom font-semibold">
                        <div className="text-[11px] leading-tight">{b.name}</div>
                        <div className="text-[9px] font-normal text-muted-foreground">≤{b.sota_ceiling}%</div>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {harnesses.map((h) => {
                    const c = HARNESS_COLORS[h.id] ?? "#64748b";
                    return (
                      <tr key={h.id} className="border-b border-border last:border-0">
                        <td className="sticky left-0 z-10 bg-card p-3">
                          <div className="flex items-center gap-2">
                            <span className="h-3 w-3 rounded-sm" style={{ background: c }} />
                            <span className="whitespace-nowrap font-medium">{h.name}</span>
                          </div>
                        </td>
                        {benchmarks.map((b) => {
                          const v = matrix[h.id]?.[b.id];
                          return (
                            <td key={b.id} className="p-1.5 text-center" style={{ background: heatBg(v, b.sota_ceiling) }}>
                              {v != null ? (
                                <span className={`text-xs font-bold tabular-nums ${ratioColor(v, b.sota_ceiling)}`}>{v}</span>
                              ) : (
                                <span className="text-muted-foreground">—</span>
                              )}
                            </td>
                          );
                        })}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </>
        )}

        {/* ================= COMBOS TAB ============ */}
        {tab === "combos" && (
          <div className="grid gap-3 md:grid-cols-2">
            {overview.best_combos.map((c, i) => {
              const col = HARNESS_COLORS[c.harness] ?? "#64748b";
              return (
                <div key={`${c.model}-${c.harness}`} className="flex items-center gap-4 rounded-xl border border-border bg-card/70 p-4 backdrop-blur">
                  <div className="w-7 text-center text-sm font-black tabular-nums text-muted-foreground">
                    {i < 3 ? <Crown className="mx-auto h-5 w-5" style={{ color: col }} fill={col} /> : i + 1}
                  </div>
                  <Cpu className="h-5 w-5 text-muted-foreground" />
                  <div className="flex-1">
                    <div className="font-semibold">{c.model}</div>
                    <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                      <span className="h-2.5 w-2.5 rounded-sm" style={{ background: col }} />
                      {harnesses.find((h) => h.id === c.harness)?.name ?? c.harness}
                    </div>
                  </div>
                  <div className="text-right">
                    <div className="text-lg font-black tabular-nums" style={{ color: col }}>{c.mean_score}%</div>
                    <div className="text-[10px] text-muted-foreground">{c.n} {T.tasks}</div>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* ---- One-click evaluate (always visible below tabs) ---- */}
        <div className="mt-8 rounded-2xl border border-border bg-gradient-to-br from-[#2DD4BF]/10 via-card/70 to-transparent p-4 backdrop-blur">
          <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
            <Play className="h-4 w-4 text-[#2DD4BF]" /> {T.evaluateTitle}
            <span className="font-normal text-muted-foreground">— {T.evaluateDesc}</span>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <select value={evalModel} onChange={(e) => setEvalModel(e.target.value)} className="rounded-lg border border-border bg-background px-3 py-2 text-sm">
              {raw.models.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
            </select>
            <span className="text-muted-foreground">×</span>
            <select value={evalHarness} onChange={(e) => setEvalHarness(e.target.value)} className="rounded-lg border border-border bg-background px-3 py-2 text-sm">
              {harnesses.map((h) => <option key={h.id} value={h.id}>{h.name}</option>)}
            </select>
            <span className="text-muted-foreground">·</span>
            <select value={evalBenchmark} onChange={(e) => setEvalBenchmark(e.target.value)} className="rounded-lg border border-border bg-background px-3 py-2 text-sm">
              {benchmarks.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
            </select>
            <button onClick={runEvaluate} disabled={evaluating} className="inline-flex items-center gap-1.5 rounded-lg bg-[#2DD4BF] px-4 py-2 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50">
              {evaluating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              {evaluating ? T.running : T.evaluate}
            </button>
            {evalMsg && <span className="text-sm text-muted-foreground">{evalMsg}</span>}
          </div>
        </div>
      </div>
    </div>
  );
}
