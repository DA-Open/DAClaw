// Official-style brand marks for LLM creators, as inline SVG so they render
// crisply at any size with no network fetch. Each returns a 24×24 <svg> whose
// artwork is the vendor's recognizable logo glyph. `color` fills monochrome
// marks; multi-color marks (Google, Meta, Mistral) ignore it.
import type { ReactNode, ReactElement } from "react";

type IconProps = { className?: string; title?: string };

function Svg({ children, title, className, viewBox = "0 0 24 24" }: { children: ReactNode; title?: string; className?: string; viewBox?: string }) {
  return (
    <svg viewBox={viewBox} className={className} role="img" aria-label={title} fill="none" xmlns="http://www.w3.org/2000/svg">
      {title ? <title>{title}</title> : null}
      {children}
    </svg>
  );
}

// OpenAI — the six-fold knot
function OpenAI({ className, title }: IconProps) {
  return (
    <Svg className={className} title={title}>
      <path fill="currentColor" d="M22.28 9.82a5.98 5.98 0 0 0-.52-4.91 6.05 6.05 0 0 0-6.51-2.9A5.98 5.98 0 0 0 10.75 0a6.06 6.06 0 0 0-5.77 4.19 5.99 5.99 0 0 0-4 2.9 6.06 6.06 0 0 0 .75 7.1 5.98 5.98 0 0 0 .51 4.91 6.05 6.05 0 0 0 6.52 2.9A5.98 5.98 0 0 0 13.26 24a6.06 6.06 0 0 0 5.77-4.2 5.99 5.99 0 0 0 4-2.9 6.06 6.06 0 0 0-.75-7.08Zm-9.02 12.6a4.48 4.48 0 0 1-2.88-1.04l.14-.08 4.78-2.76a.78.78 0 0 0 .4-.68v-6.74l2.02 1.17a.07.07 0 0 1 .04.06v5.58a4.5 4.5 0 0 1-4.5 4.49ZM3.6 18.1a4.47 4.47 0 0 1-.54-3.01l.14.08 4.78 2.76a.78.78 0 0 0 .78 0l5.84-3.37v2.33a.08.08 0 0 1-.03.07l-4.83 2.79a4.5 4.5 0 0 1-6.14-1.65ZM2.34 7.9a4.48 4.48 0 0 1 2.34-1.97v5.68a.77.77 0 0 0 .39.68l5.81 3.35-2.02 1.17a.07.07 0 0 1-.07 0l-4.83-2.79A4.5 4.5 0 0 1 2.34 7.9Zm16.6 3.86-5.84-3.4L15.11 7.2a.07.07 0 0 1 .07 0l4.83 2.78a4.49 4.49 0 0 1-.68 8.1v-5.68a.79.79 0 0 0-.4-.68Zm2.01-3.02-.14-.09-4.77-2.78a.78.78 0 0 0-.79 0L9.42 9.24V6.9a.07.07 0 0 1 .03-.06l4.83-2.79a4.5 4.5 0 0 1 6.68 4.66ZM8.32 12.86 6.3 11.7a.08.08 0 0 1-.04-.06V6.07a4.5 4.5 0 0 1 7.38-3.45l-.14.08L8.72 5.46a.78.78 0 0 0-.4.68v6.72Zm1.1-2.36L12 8.99l2.6 1.5v3l-2.6 1.5-2.6-1.5v-3Z"/>
    </Svg>
  );
}

// Anthropic (Claude) — the burst mark
function Anthropic({ className, title }: IconProps) {
  return (
    <Svg className={className} title={title}>
      <path fill="#D97757" d="M4.7 15.96 9.3 4h2.2l4.6 11.96h-2.3l-.94-2.53H7.9l-.94 2.53H4.7Zm3.86-4.38h3.28l-1.64-4.43-1.64 4.43Z"/>
      <path fill="#D97757" d="M14.66 15.96 19.26 4h2.2l-4.6 11.96h-2.2Z" opacity=".85"/>
    </Svg>
  );
}

// Google (Gemini) — the four-color "G"
function Google({ className, title }: IconProps) {
  return (
    <Svg className={className} title={title}>
      <path fill="#4285F4" d="M23.52 12.27c0-.79-.07-1.54-.2-2.27H12v4.51h6.47a5.53 5.53 0 0 1-2.4 3.58v3h3.86c2.26-2.09 3.59-5.17 3.59-8.82Z"/>
      <path fill="#34A853" d="M12 24c3.24 0 5.95-1.08 7.93-2.91l-3.86-3c-1.08.72-2.45 1.16-4.07 1.16-3.13 0-5.78-2.11-6.73-4.96H1.28v3.09A12 12 0 0 0 12 24Z"/>
      <path fill="#FBBC05" d="M5.27 14.29a7.21 7.21 0 0 1 0-4.58V6.62H1.28a12 12 0 0 0 0 10.76l3.99-3.09Z"/>
      <path fill="#EA4335" d="M12 4.75c1.77 0 3.35.6 4.6 1.8l3.42-3.42A11.96 11.96 0 0 0 12 0 12 12 0 0 0 1.28 6.62l3.99 3.09C6.22 6.86 8.87 4.75 12 4.75Z"/>
    </Svg>
  );
}

// DeepSeek — whale/wave glyph
function DeepSeek({ className, title }: IconProps) {
  return (
    <Svg className={className} title={title}>
      <path fill="#4D6BFE" d="M22.4 4.6c-.36-.18-.5.16-.72.35-.07.06-.14.14-.2.22-.55.6-1.2.99-2.05.94-1.24-.07-2.3.32-3.24 1.27-.2-1.17-.86-1.87-1.86-2.32-.53-.24-1.06-.47-1.43-.98-.26-.36-.33-.77-.46-1.16-.08-.24-.16-.48-.44-.52-.3-.05-.42.2-.54.4-.48.83-.66 1.75-.64 2.68.04 2.1.93 3.77 2.7 4.96.2.14.25.28.19.48-.12.4-.25.8-.38 1.2-.08.26-.2.31-.48.2a5.5 5.5 0 0 1-1.75-1.32c-.85-.86-1.62-1.8-2.58-2.56a10.7 10.7 0 0 0-.68-.5c-.98-.95.13-1.73.39-1.82.27-.1.09-.44-.78-.43-.87 0-1.67.29-2.68.68-.15.06-.3.1-.46.15a10.3 10.3 0 0 0-.15-.03c-.6-.36-1.24-.4-1.9-.28-1.28.22-2.36.83-3.2 1.83-1.02 1.2-1.32 2.6-1.14 4.12.19 1.6.98 2.94 2.2 3.98 1.28 1.1 2.75 1.62 4.42 1.5.99-.06 2.1-.19 3.35-1.24.31.16.64.22 1.19.27.42.04.83.08 1.24.02.65-.09.6-.7.37-.8-1.9-.9-1.48-.53-1.86-.82.98-1.15 2.46-2.35 3.03-6.24.05-.31 0-.5-.02-.75-.01-.15.03-.21.2-.23.48-.05.94-.18 1.37-.42 1.24-.7 1.74-1.85 1.86-3.18.02-.2.01-.42.22-.55.61-.36.9-.9 1.02-1.56.08-.4.15-.8-.2-.98ZM11.32 18.7a3.66 3.66 0 0 1-1.1-.98c.56.22 1.1.36 1.68.33.24-.01.48.02.72.05-.44.24-.86.44-1.3.6Zm.66-4.1a.34.34 0 0 1-.4-.03c-.35-.29-.63-.64-.83-1.06-.2-.42-.31-.83-.32-1.28 0-.19.02-.36.11-.5.16.2.29.4.44.58.36.44.7.9 1.2 1.2.3.19.31.36 0 .58l-.2.51Zm4.03-2.2c-.3.17-.66.12-.96-.05-.35-.2-.6-.5-.6-.94.31.32.62.53.99.6.36.06.55.35.57.39Z"/>
    </Svg>
  );
}

// Meta (Llama) — the infinity/Meta mark
function Meta({ className, title }: IconProps) {
  return (
    <Svg className={className} title={title} viewBox="0 0 287 191">
      <path fill="#0081FB" d="M31.06 126c0 11 2.41 19.44 5.56 24.55 4.13 6.69 10.29 9.52 16.57 9.52 8.1 0 15.51-2.01 29.79-21.76 11.44-15.83 24.92-38.05 33.99-51.98l15.36-23.6c10.67-16.39 23.02-34.61 37.18-46.96C181.17 5.65 193.78 0 206.47 0c21.31 0 41.61 12.35 57.15 35.52C280.62 60.89 288.8 92.85 288.8 125.83c0 19.61-3.87 34.02-10.45 45.41-6.36 11.02-18.76 22.02-39.62 22.02v-31.44c17.86 0 22.32-16.41 22.32-35.19 0-26.76-6.24-56.46-20-77.68-9.76-15.05-22.42-24.25-36.34-24.25-15.05 0-27.16 11.35-40.77 31.59-7.24 10.75-14.66 23.85-23 38.63l-9.06 16.05c-18.21 32.28-22.83 39.63-31.95 51.79-15.98 21.3-29.62 29.35-47.61 29.35-21.51 0-35.12-9.32-43.55-23.36C1.32 156.85 0 141.24 0 123.44l31.06 2.56Z"/>
      <path fill="#0064E1" d="M24.49 37.74C38.9 15.52 59.7 0 83.55 0c13.81 0 27.54 4.09 41.88 15.8 15.69 12.81 32.41 33.9 53.27 68.61l7.48 12.46c18.06 30.07 28.34 45.54 34.35 52.83 7.73 9.36 13.14 12.16 20.18 12.16 17.86 0 22.32-16.41 22.32-35.19l27.42-.86c0 19.61-3.87 34.02-10.45 45.41-6.36 11.02-18.76 22.02-39.62 22.02-12.97 0-24.46-2.82-37.16-14.8-9.76-9.2-21.17-25.54-29.95-40.23l-26.12-43.64c-13.1-21.9-25.12-38.22-32.08-45.62-7.49-7.95-17.11-17.55-32.45-17.55-12.41 0-22.95 8.71-31.77 22.03L24.49 37.74Z"/>
      <path fill="#0082FB" d="M83.0 30.98c-12.41 0-22.95 8.71-31.77 22.03-12.47 18.82-20.1 46.86-20.1 73.75 0 11 2.41 19.44 5.56 24.55L10.51 172.4C1.32 156.85 0 141.24 0 123.44 0 92.36 8.53 59.97 24.49 37.74 38.9 15.52 59.7 0 83.55 0l-.55 30.98Z"/>
    </Svg>
  );
}

// Alibaba Qwen — stylized mark
function Qwen({ className, title }: IconProps) {
  return (
    <Svg className={className} title={title}>
      <path fill="#615CED" d="M12 1.5 3.2 6.6v10.8l8.8 5.1 8.8-5.1V6.6L12 1.5Zm6.3 14.55L12 19.7l-6.3-3.65V8.75L12 5.1l6.3 3.65v7.3Z"/>
      <path fill="#615CED" d="m12 8-3.4 2v3.9l3.4 2 3.4-2V10L12 8Z" opacity=".7"/>
    </Svg>
  );
}

// xAI (Grok) — the X mark
function XAI({ className, title }: IconProps) {
  return (
    <Svg className={className} title={title}>
      <path fill="currentColor" d="m3.6 3 6.3 8.94L3.3 21h1.9l5.55-6.94L15.7 21H21l-6.7-9.5L20.5 3h-1.9l-5.2 6.5L8.8 3H3.6Zm2.7 1.4h2.2l10.4 15.2h-2.2L6.3 4.4Z"/>
    </Svg>
  );
}

// Z AI (GLM / Zhipu) — Z glyph
function ZAI({ className, title }: IconProps) {
  return (
    <Svg className={className} title={title}>
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#2DD4BF"/>
      <path fill="#fff" d="M7 7h10v2.2l-6.4 5.6H17V17H7v-2.2l6.4-5.6H7V7Z"/>
    </Svg>
  );
}

// Moonshot (Kimi) — crescent
function Moonshot({ className, title }: IconProps) {
  return (
    <Svg className={className} title={title}>
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#111827"/>
      <path fill="#fff" d="M15.5 6.2a6 6 0 1 0 2.3 8.1 4.8 4.8 0 0 1-2.3-8.1Z"/>
      <circle cx="9" cy="12" r="1.4" fill="#fff"/>
    </Svg>
  );
}

// MiniMax — abstract m
function MiniMax({ className, title }: IconProps) {
  return (
    <Svg className={className} title={title}>
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#FF4D4F"/>
      <path fill="#fff" d="M6 16V8h2l2 3 2-3h2v8h-2v-4.6l-2 2.9-2-2.9V16H6Zm11-8h1.5v8H17V8Z"/>
    </Svg>
  );
}

// Mistral — the pixelated flag
function Mistral({ className, title }: IconProps) {
  return (
    <Svg className={className} title={title} viewBox="0 0 24 24">
      <rect x="2" y="3" width="4" height="4" fill="#000"/>
      <rect x="18" y="3" width="4" height="4" fill="#000"/>
      <rect x="2" y="7" width="4" height="4" fill="#F7D046"/>
      <rect x="6" y="7" width="4" height="4" fill="#F7D046"/>
      <rect x="14" y="7" width="4" height="4" fill="#F7D046"/>
      <rect x="18" y="7" width="4" height="4" fill="#F7D046"/>
      <rect x="2" y="11" width="4" height="4" fill="#F2A73B"/>
      <rect x="10" y="11" width="4" height="4" fill="#F2A73B"/>
      <rect x="18" y="11" width="4" height="4" fill="#F2A73B"/>
      <rect x="2" y="15" width="4" height="4" fill="#EE792F"/>
      <rect x="18" y="15" width="4" height="4" fill="#EE792F"/>
      <rect x="6" y="15" width="8" height="4" fill="#EB5829"/>
      <rect x="2" y="19" width="8" height="2" fill="#EA3326"/>
      <rect x="14" y="19" width="8" height="2" fill="#EA3326"/>
    </Svg>
  );
}

const REGISTRY: Record<string, (p: IconProps) => ReactElement> = {
  OpenAI, Anthropic, Google, DeepSeek, Meta, Alibaba: Qwen, xAI: XAI,
  "Z AI": ZAI, "Moonshot AI": Moonshot, MiniMax, Mistral,
};

// Fallback: creator initials on a neutral chip.
function Fallback({ creator, className }: { creator: string; className?: string }) {
  return (
    <span className={`inline-flex items-center justify-center rounded-md bg-slate-500 text-[10px] font-black text-white ${className ?? ""}`}>
      {creator.slice(0, 2)}
    </span>
  );
}

export function BrandIcon({ creator, className, title }: { creator: string; className?: string; title?: string }) {
  const Cmp = REGISTRY[creator];
  if (Cmp) return <Cmp className={className} title={title ?? creator} />;
  return <Fallback creator={creator} className={className} />;
}

export function hasBrandIcon(creator: string): boolean {
  return creator in REGISTRY;
}
