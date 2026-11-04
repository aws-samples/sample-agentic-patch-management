import { useState, useEffect, useCallback } from "react";
import { initialTheme, type Theme } from "../lib/view";

export type { Theme };

const THEME_KEY = "patchy-theme";

function stored(): string | null {
  try {
    return localStorage.getItem(THEME_KEY);
  } catch {
    return null;
  }
}

function prefersDark(): boolean {
  return typeof window !== "undefined" && !!window.matchMedia?.("(prefers-color-scheme: dark)").matches;
}

/** Light or dark: the choice remembered in this browser, else the operating system's setting. */
export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(() => initialTheme(stored(), prefersDark()));

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  // Follow the operating system until the user picks a theme here.
  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mq) return;
    const follow = (e: MediaQueryListEvent) => { if (!stored()) setThemeState(e.matches ? "dark" : "light"); };
    mq.addEventListener("change", follow);
    // A choice made in another Patchy window applies here too.
    const fromOther = (e: StorageEvent) => {
      if (e.key === THEME_KEY && (e.newValue === "light" || e.newValue === "dark")) setThemeState(e.newValue);
    };
    window.addEventListener("storage", fromOther);
    return () => {
      mq.removeEventListener("change", follow);
      window.removeEventListener("storage", fromOther);
    };
  }, []);

  const setTheme = useCallback((t: Theme) => {
    setThemeState(t);
    try { localStorage.setItem(THEME_KEY, t); } catch { /* best-effort */ }
  }, []);

  const toggle = useCallback(() => setTheme(theme === "dark" ? "light" : "dark"), [theme, setTheme]);

  return { theme, setTheme, toggle };
}
