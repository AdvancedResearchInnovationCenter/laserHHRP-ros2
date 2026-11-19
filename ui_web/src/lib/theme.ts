// Light / dark theme: a data-theme attribute on <html> (styles.css), remembered in this browser only.
// Storage can be missing or blocked (private window, cleared profile): then the UI starts dark.
export type Theme = "dark" | "light";
const KEY = "hhrp-theme";

export function loadTheme(): Theme {
  try {
    return localStorage.getItem(KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

export function saveTheme(theme: Theme): void {
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    // not remembered; the switch still applies until the page reloads
  }
}

export function applyTheme(theme: Theme): void {
  if (typeof document !== "undefined") document.documentElement.dataset.theme = theme;
}

export function currentTheme(): Theme {
  return typeof document !== "undefined" && document.documentElement.dataset.theme === "light" ? "light" : "dark";
}
