export const THEME_KEY = "selfreply-theme";
export const THEME_CYCLE = ["auto", "light", "dark"];
export const THEME_LABELS = {
  auto: "跟随系统",
  light: "浅色 · 慈爱之惠",
  dark: "深色 · 审判之司",
};
export function currentTheme() {
  const value = document.documentElement.getAttribute("data-theme");
  return value === "light" || value === "dark" ? value : "auto";
}
export function cacheThemeLocally(theme) {
  try {
    if (theme === "auto") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* iframe 环境 localStorage 不可用 */
  }
}
export function applyTheme(theme, themeToggle) {
  if (theme === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", theme);
  cacheThemeLocally(theme);
  if (themeToggle) {
    themeToggle.setAttribute(
      "aria-label",
      `切换主题，当前：${THEME_LABELS[theme] || THEME_LABELS.auto}`
    );
  }
}
export async function persistTheme(theme, apiPost) {
  cacheThemeLocally(theme);
  try {
    await apiPost("ui/theme", { theme });
  } catch {
    /* 后端持久化失败仅当次生效 */
  }
}
export async function restoreTheme(apiGet) {
  try {
    const result = await apiGet("ui/theme");
    const saved =
      result && result.ok !== false ? String(result.theme || "auto").trim() : "auto";
    return saved === "light" || saved === "dark" ? saved : "auto";
  } catch {
    return currentTheme();
  }
}
export function nextTheme() {
  return THEME_CYCLE[(THEME_CYCLE.indexOf(currentTheme()) + 1) % THEME_CYCLE.length];
}
