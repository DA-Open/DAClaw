// A polished assistant avatar — a soft teal gradient rounded-square with a
// clean robot glyph. Replaces the plain lucide <Bot/> which looked flat.

export function BotAvatar({ size = 28, className = "" }: { size?: number; className?: string }) {
  return (
    <span
      className={`inline-flex shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-[#2DD4BF] to-[#0EA5A3] text-white shadow-sm ${className}`}
      style={{ width: size, height: size }}
      aria-hidden
    >
      <svg
        width={Math.round(size * 0.62)}
        height={Math.round(size * 0.62)}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.9}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        {/* antenna */}
        <path d="M12 3v2.2" />
        <circle cx="12" cy="2.4" r="1" fill="currentColor" stroke="none" />
        {/* head */}
        <rect x="4.5" y="5.5" width="15" height="11" rx="3.2" />
        {/* eyes */}
        <circle cx="9" cy="11" r="1.15" fill="currentColor" stroke="none" />
        <circle cx="15" cy="11" r="1.15" fill="currentColor" stroke="none" />
        {/* smile */}
        <path d="M9.2 13.6c.9.8 4.7.8 5.6 0" />
      </svg>
    </span>
  );
}
