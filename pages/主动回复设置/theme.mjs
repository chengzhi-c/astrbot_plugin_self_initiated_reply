import { boldWasTouched, dimWasTouched } from "./chrome.mjs";

export const THEME_KEY = "selfreply-theme";
const THEME_CYCLE = ["auto", "light", "dark"];
// 主题合法值的单点判定：index.html 的首帧内联脚本在模块加载前执行，拿不到这里，
// 那是本集合仅剩的第二份副本。
export function normalizeTheme(value) {
  const text = String(value ?? "").trim();
  return THEME_CYCLE.includes(text) ? text : "auto";
}
const THEME_LABELS = {
  auto: "跟随系统",
  light: "慈爱之惠",
  dark: "审判之司",
};
// 读屏播报需要色向：短标签本身不含浅/深，凭 aria-label 才知道当前是哪一档。
const THEME_ARIA_PREFIX = { light: "浅色 · ", dark: "深色 · " };
function themeAriaLabel(theme) {
  const label = THEME_LABELS[theme] || THEME_LABELS.auto;
  return `切换主题，当前：${THEME_ARIA_PREFIX[theme] || ""}${label}`;
}
export function currentTheme() {
  return normalizeTheme(document.documentElement.getAttribute("data-theme"));
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
  if (theme === "auto") {
    document.documentElement.removeAttribute("data-theme");
  } else {
    document.documentElement.setAttribute("data-theme", theme);
  }
  cacheThemeLocally(theme);
  if (!themeToggle) return;
  themeToggle.setAttribute("aria-label", themeAriaLabel(theme));
  // 主题名的唯一副本在本映射里：按钮里的可见文字也由这里写，CSS 不再用
  // content 声明标签，否则两侧文案各自漂移而无人报错。
  const label = themeToggle.querySelector(".theme-label");
  if (label) label.textContent = THEME_LABELS[theme] || THEME_LABELS.auto;
}
// 主题写入必须串行：连点两次会各发一个 POST，后端按到达顺序落盘，先发的那笔
// 若后到就把用户最后一次选择覆盖回旧值（刷新后主题静默回退）。排同一条 promise
// 链尾，才能保证「最后点击的那次」也是最后落盘的那次。
let themeWriteChain = Promise.resolve();
async function sendThemeWrite(body, apiPost, onPersistFailure) {
  try {
    // 后端拒绝走的是 HTTP 200 + {ok:false}（插件正在关闭 / 主题写入失败 /
    // 请求体不是合法 JSON，见 webapi._api_post_ui_theme），而 apiPost 只对
    // 非 2xx 抛异常。只 try/catch 会把「服务端明确拒绝」当成成功——
    // 与 transport 失败同一副用户可见后果（下次加载静默回退），必须一起反馈。
    const result = await apiPost("ui/theme", body);
    if (result && result.ok === false) {
      throw new Error(result.error || "主题偏好写入失败");
    }
  } catch (err) {
    // 不能吐：cacheThemeLocally 已把选择写进本地缓存，本次会话看起来完全成功。
    // 真正的代价在下一次加载：restoreTheme 会把服务器上的旧值覆盖回去，
    // 运营者的选择静默回退且没有任何解释。故此处必须给出可见反馈。
    // onPersistFailure 由调用方注入（app.js / chrome.mjs 均传 showToast）；
    // 不给默认值是为了让新调用方明确选择怎么告诉用户，
    // 而不是默默恢复成“什么都不说”。
    if (typeof onPersistFailure === "function") {
      onPersistFailure(
        "主题偏好保存失败，将在下次启动时回退",
        err,
      );
    }
  }
}
export function persistTheme(theme, apiPost, onPersistFailure) {
  // 三个字段各自只在「用户真的动过它」时才提交：后端对未提交的键保持原值，
  // 而渲染态在 GET ui/theme 返回前是本地默认（theme 恒为 "auto"、
  // dim/bold 恒为 false），把它一并提交会把服务端已存的选择静默改掉。
  // 总判据（任一被动过就两个都提交）不够：GET 在途时点一下压暗，DOM 里的
  // 粗体还是本地默认 false，一并提交就把服务端已存的 bold 抹成 false。
  const body = {};
  if (theme) {
    cacheThemeLocally(theme);
    body.theme = theme;
  }
  if (dimWasTouched()) {
    body.dim = document.documentElement.classList.contains("dimmed");
  }
  if (boldWasTouched()) {
    body.bold = document.documentElement.classList.contains("bold-text");
  }
  if (Object.keys(body).length === 0) return Promise.resolve();
  // 请求体在调用时定格：判据是「这次点击时用户动过哪些键」，排队期间 DOM 与
  // 触摸态的变化属于后面那次点击的意图。
  const queued = themeWriteChain.then(() => sendThemeWrite(body, apiPost, onPersistFailure));
  // 单笔写入的异常不能永久卡死队列：链尾吸收掉（失败反馈已由 sendThemeWrite
  // 负责），而调用方拿到的仍是这次写入自己的 promise。
  themeWriteChain = queued.catch(() => {});
  return queued;
}
export async function restoreTheme(apiGet) {
  try {
    const result = await apiGet("ui/theme");
    const stored = result && result.ok !== false ? result.theme : "";
    const resolved = normalizeTheme(stored);
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
