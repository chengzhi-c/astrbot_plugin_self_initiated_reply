export const THEME_KEY = "selfreply-theme";
const THEME_CYCLE = ["auto", "light", "dark"];
export const THEME_LABELS = {
  auto: "跟随系统",
  light: "浅色 · 慈爱之惠",
  dark: "深色 · 审判之司",
};
export function currentTheme() {
  const value = document.documentElement.getAttribute("data-theme");
  return value === "light" || value === "dark" ? value : "auto";
}
function cacheThemeLocally(theme) {
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
  // theme 省略 = 只改压暗/粗体：渲染态尚未反映服务端主题（GET 在途或
  // localStorage 不可用）时 currentTheme() 恒为 "auto"，把它一并提交会把
  // 服务端已存的 light/dark 静默改成跟随系统。后端对未提交的键保持原值。
  if (theme) cacheThemeLocally(theme);
  try {
    await apiPost("ui/theme", {
      ...(theme ? { theme } : {}),
      dim: document.documentElement.classList.contains("dimmed"),
      bold: document.documentElement.classList.contains("bold-text"),
    });
  } catch {
    /* 后端持久化失败仅当次生效 */
  }
}
export async function restoreTheme(apiGet) {
  try {
    const result = await apiGet("ui/theme");
    const saved =
      result && result.ok !== false ? String(result.theme || "auto").trim() : "auto";
    const resolved = saved === "light" || saved === "dark" ? saved : "auto";
    /* 本地缓存由调用方守卫后的 applyTheme 落盘：此处若直写，在途响应会把用户
       刚点击的主题从 localStorage 覆盖回旧值（下一次冷启动主题回退）。 */
    return {
      theme: resolved,
      dim: Boolean(result && result.dim),
      bold: Boolean(result && result.bold),
    };
  } catch {
    return {
      theme: currentTheme(),
      dim: document.documentElement.classList.contains("dimmed"),
      bold: document.documentElement.classList.contains("bold-text"),
    };
  }
}
export function nextTheme() {
  return THEME_CYCLE[(THEME_CYCLE.indexOf(currentTheme()) + 1) % THEME_CYCLE.length];
}
