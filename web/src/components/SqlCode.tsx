import { useMemo } from "react";

// Lightweight SQL pretty-print + syntax highlight for the ETL node inspector.
// Mirrors the server-side viz.render_sql_block styling (dark block, keyword/
// string/number colors, clause line-breaks). Pure presentational — no deps.

const NEWLINE_KW = [
  "WITH", "SELECT", "FROM", "WHERE", "GROUP BY", "ORDER BY", "HAVING",
  "LIMIT", "LEFT JOIN", "RIGHT JOIN", "INNER JOIN", "FULL JOIN",
  "CROSS JOIN", "JOIN", "UNION ALL", "UNION",
];
const KEYWORDS = [
  "WITH", "SELECT", "DISTINCT", "FROM", "WHERE", "GROUP BY", "ORDER BY",
  "HAVING", "LIMIT", "OFFSET", "LEFT JOIN", "RIGHT JOIN", "INNER JOIN",
  "FULL JOIN", "CROSS JOIN", "JOIN", "ON", "USING", "AND", "OR", "NOT",
  "AS", "CASE", "WHEN", "THEN", "ELSE", "END", "UNION ALL", "UNION", "OVER",
  "PARTITION BY", "VALUES", "IN", "IS", "NULL", "ASC", "DESC", "BETWEEN", "LIKE",
];

function formatSql(sql: string): string {
  let s = (sql || "").replace(/\s+/g, " ").trim();
  if (!s) return "";
  for (const kw of [...NEWLINE_KW].sort((a, b) => b.length - a.length)) {
    s = s.replace(new RegExp(`\\s?\\b${kw}\\b`, "gi"), "\n" + kw);
  }
  return s.split("\n").map((l) => l.trim()).filter(Boolean).join("\n");
}

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function highlight(sql: string): string {
  let h = esc(formatSql(sql));
  // strings
  h = h.replace(/('[^']*')/g, '<span class="s-str">$1</span>');
  // numbers
  h = h.replace(/\b(\d+\.?\d*)\b/g, '<span class="s-num">$1</span>');
  // keywords
  for (const kw of [...KEYWORDS].sort((a, b) => b.length - a.length)) {
    h = h.replace(new RegExp(`(?<![\\w])(${kw})(?![\\w])`, "gi"), '<span class="s-kw">$1</span>');
  }
  return h;
}

export function SqlCode({ sql, className = "" }: { sql: string; className?: string }) {
  const html = useMemo(() => highlight(sql), [sql]);
  return (
    <pre
      className={`overflow-x-auto whitespace-pre-wrap break-words rounded-lg bg-[#0f172a] p-3 font-mono-ui text-[11.5px] leading-relaxed text-slate-200 [&_.s-kw]:font-semibold [&_.s-kw]:text-sky-300 [&_.s-num]:text-amber-300 [&_.s-str]:text-red-300 ${className}`}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
