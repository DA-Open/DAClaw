import { Languages } from "lucide-react";
import { useI18n } from "@/i18n/context";

export function LanguageSwitcher() {
  const { locale, setLocale } = useI18n();
  const nextLocale = locale === "en" ? "zh" : "en";
  const label = locale === "en" ? "中文" : "EN";
  const title = locale === "en" ? "切换到中文" : "Switch to English";

  return (
    <button
      type="button"
      onClick={() => setLocale(nextLocale)}
      className="inline-flex h-9 items-center gap-1.5 rounded-full border border-border bg-background px-3 text-sm font-medium text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
      title={title}
      aria-label={title}
    >
      <Languages className="h-4 w-4" />
      <span>{label}</span>
    </button>
  );
}
