"use client";

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

export type Theme = "light" | "dark";

const STORAGE_KEY = "drcc-theme";

interface ThemeContextValue {
  theme: Theme;
  setTheme: (theme: Theme) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function applyThemeClass(theme: Theme) {
  document.documentElement.classList.toggle("dark", theme === "dark");
}

/**
 * Persists to localStorage (allowed for theme preference — nextjs-web rule).
 *
 * Initial state is always "light" so the first client render matches the
 * server-rendered HTML exactly (no hydration mismatch); the pre-hydration
 * `ThemeInitScript` already applies the real `dark` class to <html> before
 * paint so there is no visible flash. The one-time effect below then
 * reconciles React state with the persisted preference — a deliberate,
 * unavoidable exception to "don't setState synchronously in an effect": the
 * real value only exists in browser storage, which isn't available during SSR.
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>("light");

  useEffect(() => {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === "dark") {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- reconciling with browser-only storage after mount, see comment above
      setThemeState("dark");
    }
  }, []);

  useEffect(() => {
    applyThemeClass(theme);
  }, [theme]);

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next);
    window.localStorage.setItem(STORAGE_KEY, next);
  }, []);

  return <ThemeContext.Provider value={{ theme, setTheme }}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) {
    throw new Error("useTheme must be used within a ThemeProvider");
  }
  return ctx;
}

/** Inline, pre-hydration script: reads localStorage and sets the class before
 * first paint so there is no light-mode flash for Dark-mode users. */
export function ThemeInitScript() {
  const script = `(function(){try{var t=window.localStorage.getItem(${JSON.stringify(
    STORAGE_KEY,
  )});if(t==="dark"){document.documentElement.classList.add("dark");}}catch(e){}})();`;
  return <script dangerouslySetInnerHTML={{ __html: script }} />;
}
