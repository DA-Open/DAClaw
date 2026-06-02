import { useCallback, useEffect, useState } from "react";

export type ThemePreference = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

const THEME_STORAGE_KEY = "daclaw-theme-preference";
const THEME_EVENT = "daclaw-theme-preference-change";

function isThemePreference(value: string | null): value is ThemePreference {
  return value === "system" || value === "light" || value === "dark";
}

function systemTheme(): ResolvedTheme {
  if (typeof window === "undefined" || !window.matchMedia) return "light";
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function getStoredThemePreference(): ThemePreference {
  if (typeof localStorage === "undefined") return "system";
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    return isThemePreference(stored) ? stored : "system";
  } catch {
    return "system";
  }
}

export function resolveThemePreference(preference: ThemePreference): ResolvedTheme {
  return preference === "system" ? systemTheme() : preference;
}

export function applyThemePreference(preference: ThemePreference = getStoredThemePreference()): ResolvedTheme {
  const resolved = resolveThemePreference(preference);
  if (typeof document !== "undefined") {
    const root = document.documentElement;
    root.classList.toggle("dark", resolved === "dark");
    root.dataset.theme = resolved;
    root.dataset.themePreference = preference;
  }
  return resolved;
}

export function setStoredThemePreference(preference: ThemePreference) {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, preference);
  } catch {
    // Ignore storage failures in private browsing or locked-down environments.
  }
  applyThemePreference(preference);
  window.dispatchEvent(new CustomEvent<ThemePreference>(THEME_EVENT, { detail: preference }));
}

export function useThemePreference() {
  const [preference, setPreferenceState] = useState<ThemePreference>(getStoredThemePreference);
  const [resolvedTheme, setResolvedTheme] = useState<ResolvedTheme>(() => applyThemePreference(preference));

  const syncPreference = useCallback((next: ThemePreference) => {
    setPreferenceState(next);
    setResolvedTheme(applyThemePreference(next));
  }, []);

  useEffect(() => {
    syncPreference(preference);
  }, [preference, syncPreference]);

  useEffect(() => {
    const handleSystemChange = () => {
      const next = getStoredThemePreference();
      if (next === "system") syncPreference(next);
    };
    const handleStorage = (event: StorageEvent) => {
      if (event.key === THEME_STORAGE_KEY) syncPreference(getStoredThemePreference());
    };
    const handleCustom = (event: Event) => {
      const next = (event as CustomEvent<ThemePreference>).detail;
      if (isThemePreference(next)) syncPreference(next);
    };

    const media = window.matchMedia?.("(prefers-color-scheme: dark)");
    media?.addEventListener?.("change", handleSystemChange);
    window.addEventListener("storage", handleStorage);
    window.addEventListener(THEME_EVENT, handleCustom);

    return () => {
      media?.removeEventListener?.("change", handleSystemChange);
      window.removeEventListener("storage", handleStorage);
      window.removeEventListener(THEME_EVENT, handleCustom);
    };
  }, [syncPreference]);

  const setPreference = useCallback((next: ThemePreference) => {
    setStoredThemePreference(next);
    syncPreference(next);
  }, [syncPreference]);

  return { preference, resolvedTheme, setPreference };
}
