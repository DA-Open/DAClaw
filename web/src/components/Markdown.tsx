import { useMemo } from "react";
import katex from "katex";
import "katex/dist/katex.min.css";

/**
 * Lightweight markdown renderer for LLM output.
 * Supports the report shapes DAClaw commonly emits: headings, lists, tables,
 * code blocks, links, inline styles, and LaTeX-style math blocks.
 */
export function Markdown({ content, highlightTerms }: { content: string; highlightTerms?: string[] }) {
  const blocks = useMemo(() => parseBlocks(content), [content]);

  return (
    <div className="space-y-4 text-[15px] leading-7 text-foreground">
      {blocks.map((block, i) => (
        <Block key={i} block={block} highlightTerms={highlightTerms} />
      ))}
    </div>
  );
}

type BlockNode =
  | { type: "code"; lang: string; content: string }
  | { type: "heading"; level: number; content: string }
  | { type: "hr" }
  | { type: "list"; ordered: boolean; items: string[] }
  | { type: "table"; headers: string[]; rows: string[][] }
  | { type: "math"; content: string }
  | { type: "paragraph"; content: string };

function parseBlocks(text: string): BlockNode[] {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const blocks: BlockNode[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    const fenceMatch = line.match(/^```(\w*)/);
    if (fenceMatch) {
      const lang = fenceMatch[1] || "";
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) {
        codeLines.push(lines[i]);
        i++;
      }
      if (i < lines.length) i++;
      blocks.push({ type: "code", lang, content: codeLines.join("\n") });
      continue;
    }

    if (isMathStart(trimmed)) {
      const collected = collectMathBlock(lines, i);
      blocks.push({ type: "math", content: collected.content });
      i = collected.next;
      continue;
    }

    if (isTableStart(lines, i)) {
      const collected = collectTable(lines, i);
      blocks.push(collected.table);
      i = collected.next;
      continue;
    }

    const headingMatch = line.match(/^(#{1,4})\s+(.+)/);
    if (headingMatch) {
      blocks.push({ type: "heading", level: headingMatch[1].length, content: headingMatch[2] });
      i++;
      continue;
    }

    if (/^[-*_]{3,}\s*$/.test(line)) {
      blocks.push({ type: "hr" });
      i++;
      continue;
    }

    if (/^[-*+]\s/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^[-*+]\s/.test(lines[i])) {
        items.push(lines[i].replace(/^[-*+]\s/, ""));
        i++;
      }
      blocks.push({ type: "list", ordered: false, items });
      continue;
    }

    if (/^\d+[.)]\s/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\d+[.)]\s/.test(lines[i])) {
        items.push(lines[i].replace(/^\d+[.)]\s/, ""));
        i++;
      }
      blocks.push({ type: "list", ordered: true, items });
      continue;
    }

    if (trimmed === "") {
      i++;
      continue;
    }

    const paraLines: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !lines[i].match(/^```/) &&
      !isMathStart(lines[i].trim()) &&
      !isTableStart(lines, i) &&
      !lines[i].match(/^#{1,4}\s/) &&
      !lines[i].match(/^[-*+]\s/) &&
      !lines[i].match(/^\d+[.)]\s/) &&
      !lines[i].match(/^[-*_]{3,}\s*$/)
    ) {
      paraLines.push(lines[i]);
      i++;
    }
    if (paraLines.length > 0) {
      blocks.push({ type: "paragraph", content: paraLines.join("\n") });
    }
  }

  return blocks;
}

function isMathStart(trimmed: string): boolean {
  return trimmed === "\\[" || trimmed.startsWith("\\[") || trimmed === "$$" || trimmed.startsWith("$$");
}

function collectMathBlock(lines: string[], start: number): { content: string; next: number } {
  const first = lines[start].trim();
  const close = first.startsWith("$$") ? "$$" : "\\]";
  const open = first.startsWith("$$") ? "$$" : "\\[";
  const parts: string[] = [];

  let firstContent = first.slice(open.length).trim();
  if (firstContent.endsWith(close) && firstContent.length > close.length) {
    firstContent = firstContent.slice(0, -close.length).trim();
    return { content: firstContent, next: start + 1 };
  }
  if (firstContent) parts.push(firstContent);

  let i = start + 1;
  while (i < lines.length) {
    const line = lines[i];
    const closeIndex = line.indexOf(close);
    if (closeIndex >= 0) {
      const before = line.slice(0, closeIndex).trim();
      if (before) parts.push(before);
      i++;
      break;
    }
    parts.push(line);
    i++;
  }

  return { content: parts.join("\n").trim(), next: i };
}

function isTableStart(lines: string[], index: number): boolean {
  if (index + 1 >= lines.length) return false;
  const header = lines[index].trim();
  const divider = lines[index + 1].trim();
  return (
    header.includes("|") &&
    divider.includes("|") &&
    /^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$/.test(divider)
  );
}

function splitTableRow(row: string): string[] {
  const trimmed = row.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split("|").map((cell) => cell.trim());
}

function collectTable(lines: string[], start: number): { table: Extract<BlockNode, { type: "table" }>; next: number } {
  const headers = splitTableRow(lines[start]);
  const rows: string[][] = [];
  let i = start + 2;
  while (i < lines.length && lines[i].trim().includes("|") && lines[i].trim() !== "") {
    rows.push(splitTableRow(lines[i]));
    i++;
  }
  return { table: { type: "table", headers, rows }, next: i };
}

function Block({ block, highlightTerms }: { block: BlockNode; highlightTerms?: string[] }) {
  switch (block.type) {
    case "code":
      return (
        <pre className="overflow-x-auto rounded-xl border border-border bg-secondary/70 px-4 py-3 text-xs leading-relaxed">
          <code className="bg-transparent p-0">{block.content}</code>
        </pre>
      );

    case "math":
      return (
        <div className="overflow-x-auto rounded-2xl border border-border bg-secondary/45 px-4 py-3">
          <MathContent content={block.content} displayMode />
        </div>
      );

    case "heading": {
      const Tag = `h${Math.min(block.level, 4)}` as "h1" | "h2" | "h3" | "h4";
      const sizes: Record<string, string> = {
        h1: "mt-2 border-b border-border pb-3 text-2xl font-semibold tracking-tight",
        h2: "mt-6 border-b border-border pb-2 text-xl font-semibold tracking-tight",
        h3: "mt-5 text-lg font-semibold",
        h4: "mt-4 text-base font-semibold",
      };
      return (
        <Tag className={sizes[Tag]}>
          <InlineContent text={block.content} highlightTerms={highlightTerms} />
        </Tag>
      );
    }

    case "hr":
      return <hr className="border-border" />;

    case "list": {
      const Tag = block.ordered ? "ol" : "ul";
      return (
        <Tag className={`space-y-1 ${block.ordered ? "list-decimal" : "list-disc"} pl-6`}>
          {block.items.map((item, i) => (
            <li key={i} className="pl-1">
              <InlineContent text={item} highlightTerms={highlightTerms} />
            </li>
          ))}
        </Tag>
      );
    }

    case "table":
      return (
        <div className="overflow-x-auto rounded-2xl border border-border bg-card shadow-sm">
          <table className="min-w-full border-collapse text-left text-sm">
            <thead className="bg-secondary/70 text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                {block.headers.map((header, i) => (
                  <th key={i} className="border-b border-border px-4 py-3 font-semibold">
                    <InlineContent text={header} highlightTerms={highlightTerms} />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr key={rowIndex} className="border-b border-border last:border-0 hover:bg-secondary/40">
                  {block.headers.map((_, cellIndex) => (
                    <td key={cellIndex} className="px-4 py-3 align-top">
                      <InlineContent text={row[cellIndex] || ""} highlightTerms={highlightTerms} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );

    case "paragraph":
      return (
        <p className="whitespace-pre-wrap break-words">
          <InlineContent text={block.content} highlightTerms={highlightTerms} />
        </p>
      );
  }
}

type InlineNode =
  | { type: "text"; content: string }
  | { type: "code"; content: string }
  | { type: "math"; content: string }
  | { type: "bold"; content: string }
  | { type: "italic"; content: string }
  | { type: "link"; text: string; href: string }
  | { type: "br" };

function parseInline(text: string): InlineNode[] {
  const nodes: InlineNode[] = [];
  const pattern =
    /(\*\*([\s\S]+?)\*\*)|(`[^`\n]+`)|(\\\((.*?)\\\))|(\$([^$\n]+)\$)|(\[([^\]]+)\]\(([^)]+)\))|(\*([^*\n]+)\*)|(\bhttps?:\/\/[^\s<>)\]]+)|(\n)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push({ type: "text", content: text.slice(lastIndex, match.index) });
    }

    if (match[1]) {
      nodes.push({ type: "bold", content: match[2] });
    } else if (match[3]) {
      nodes.push({ type: "code", content: match[3].slice(1, -1) });
    } else if (match[4]) {
      nodes.push({ type: "math", content: match[5] });
    } else if (match[6]) {
      if (looksLikeInlineMath(match[7])) {
        nodes.push({ type: "math", content: match[7] });
      } else {
        nodes.push({ type: "text", content: match[6] });
      }
    } else if (match[8]) {
      nodes.push({ type: "link", text: match[9], href: match[10] });
    } else if (match[11]) {
      nodes.push({ type: "italic", content: match[12] });
    } else if (match[13]) {
      nodes.push({ type: "link", text: match[13], href: match[13] });
    } else if (match[14]) {
      nodes.push({ type: "br" });
    }

    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length) {
    nodes.push({ type: "text", content: text.slice(lastIndex) });
  }

  return nodes;
}

function looksLikeInlineMath(value: string): boolean {
  const text = value.trim();
  if (!text) return false;
  if (/\\|[{}_^=]|(?:\s|^)[+\-*/](?:\s|$)/.test(text)) return true;
  if (/^[a-zA-Z][a-zA-Z0-9]?$/.test(text)) return true;
  return false;
}

function InlineContent({ text, highlightTerms }: { text: string; highlightTerms?: string[] }) {
  const nodes = useMemo(() => parseInline(text), [text]);

  return (
    <>
      {nodes.map((node, i) => {
        switch (node.type) {
          case "text":
            return <HighlightedText key={i} text={node.content} terms={highlightTerms} />;
          case "code":
            return (
              <code key={i} className="rounded-md bg-secondary/80 px-1.5 py-0.5 font-mono-ui text-[0.86em] text-primary/90">
                {node.content}
              </code>
            );
          case "math":
            return <MathContent key={i} content={node.content} />;
          case "bold":
            return (
              <strong key={i} className="font-semibold">
                <InlineContent text={node.content} highlightTerms={highlightTerms} />
              </strong>
            );
          case "italic":
            return (
              <em key={i}>
                <HighlightedText text={node.content} terms={highlightTerms} />
              </em>
            );
          case "link":
            return (
              <a
                key={i}
                href={node.href}
                target="_blank"
                rel="noreferrer"
                className="text-primary underline underline-offset-2 decoration-primary/30 transition-colors hover:decoration-primary/70"
              >
                {node.text}
              </a>
            );
          case "br":
            return <br key={i} />;
        }
      })}
    </>
  );
}

function MathContent({ content, displayMode = false }: { content: string; displayMode?: boolean }) {
  const html = useMemo(() => {
    try {
      return katex.renderToString(content, {
        displayMode,
        throwOnError: false,
        strict: false,
        trust: false,
        output: "html",
      });
    } catch {
      return "";
    }
  }, [content, displayMode]);

  if (!html) {
    return (
      <code className="rounded-md bg-secondary/80 px-1.5 py-0.5 font-mono-ui text-[0.86em] text-primary/90">
        {content}
      </code>
    );
  }

  if (displayMode) {
    return <div className="katex-block text-foreground" dangerouslySetInnerHTML={{ __html: html }} />;
  }

  return <span className="katex-inline text-foreground" dangerouslySetInnerHTML={{ __html: html }} />;
}

function HighlightedText({ text, terms }: { text: string; terms?: string[] }) {
  if (!terms || terms.length === 0) return <>{text}</>;

  const escaped = terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const regex = new RegExp(`(${escaped.join("|")})`, "gi");
  const testRegex = new RegExp(`^(${escaped.join("|")})$`, "i");
  const parts = text.split(regex);

  return (
    <>
      {parts.map((part, i) =>
        testRegex.test(part) ? (
          <mark key={i} className="bg-warning/20 px-0.5 text-warning">
            {part}
          </mark>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </>
  );
}
