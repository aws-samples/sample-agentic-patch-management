import { useState, useEffect, useCallback } from "react";

export type Theme = "dark" | "light";

const THEME_KEY = "patchy-theme";

function getStoredTheme(): Theme {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    if (stored === "light") return "light";
  } catch {}
  return "dark";
}

export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(getStoredTheme);

  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle("light", theme === "light");
    root.classList.toggle("dark", theme === "dark");
  }, [theme]);

  const setTheme = useCallback((t: Theme) => {
    setThemeState(t);
    try { localStorage.setItem(THEME_KEY, t); } catch {}
  }, []);

  const toggle = useCallback(() => {
    setTheme(theme === "dark" ? "light" : "dark");
  }, [theme, setTheme]);

  return { theme, setTheme, toggle };
}
