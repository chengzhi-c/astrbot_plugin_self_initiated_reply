const PLUGIN_ID = "astrbot_plugin_self_initiated_reply";

let els = null;

function getEls() {
  if (els) return els;
  els = {
    topbar: document.querySelector(".topbar"),
    scrollProgress: document.getElementById("scrollProgress"),
    sidenav: document.getElementById("sidenav"),
    navSaveDot: document.getElementById("navSaveDot"),
    navSaveState: document.getElementById("navSaveState"),
    refreshBtn: document.getElementById("refreshBtn"),
    saveTopBtn: document.getElementById("saveTopBtn"),
    themeToggle: document.getElementById("themeToggle"),
    selfStat: document.getElementById("selfStat"),
    selfStatus: document.getElementById("selfStatus"),
    decisionModelStat: document.getElementById("decisionModelStat"),
    decisionModelStatus: document.getElementById("decisionModelStatus"),
    whitelistCount: document.getElementById("whitelistCount"),
    configForm: document.getElementById("configForm"),
    enabledInput: document.getElementById("enabledInput"),
    decisionModelInput: document.getElementById("decisionModelInput"),
    providerField: document.getElementById("judgeProviderField"),
    judgeProviderSelect: document.getElementById("judgeProviderSelect"),
    judgeProviderInput: document.getElementById("judgeProviderInput"),
    providerManualBtn: document.getElementById("providerManualBtn"),
    visionProviderSelect: document.getElementById("visionProviderSelect"),
    visionProviderInput: document.getElementById("visionProviderInput"),
    visionProviderManualBtn: document.getElementById("visionProviderManualBtn"),
    visionJudgeProviderSelect: document.getElementById("visionJudgeProviderSelect"),
    visionJudgeProviderInput: document.getElementById("visionJudgeProviderInput"),
    visionJudgeProviderManualBtn: document.getElementById("visionJudgeProviderManualBtn"),
    providerHint: document.getElementById("providerHint"),
    decisionTempInput: document.getElementById("decisionTempInput"),
    decisionTimeoutInput: document.getElementById("decisionTimeoutInput"),
    decisionPromptInput: document.getElementById("decisionPromptInput"),
    promptPreview: document.getElementById("promptPreview"),
    resetPromptBtn: document.getElementById("resetPromptBtn"),
    minContextInput: document.getElementById("minContextInput"),
    messageDelayInput: document.getElementById("messageDelayInput"),
    minSilenceInput: document.getElementById("minSilenceInput"),
    cooldownInput: document.getElementById("cooldownInput"),
    visionJudgeEnabledInput: document.getElementById("visionJudgeEnabledInput"),
    visionMainEnabledInput: document.getElementById("visionMainEnabledInput"),
    visionSkipStickersInput: document.getElementById("visionSkipStickersInput"),
    visionMaxImagesInput: document.getElementById("visionMaxImagesInput"),
    visionImageAgeInput: document.getElementById("visionImageAgeInput"),
    visionTimeoutInput: document.getElementById("visionTimeoutInput"),
    proactiveInheritToolsInput: document.getElementById("proactiveInheritToolsInput"),
    cleanupImageCacheBtn: document.getElementById("cleanupImageCacheBtn"),
    cleanupImageCacheState: document.getElementById("cleanupImageCacheState"),
    whitelistInput: document.getElementById("whitelistInput"),
    whitelistError: document.getElementById("whitelistError"),
    configSaveState: document.getElementById("configSaveState"),
    toast: document.getElementById("toast"),
    boot: document.getElementById("boot"),
    mobileSaveBar: document.getElementById("mobileSaveBar"),
    mobileSaveState: document.getElementById("mobileSaveState"),
    saveMobileBtn: document.getElementById("saveMobileBtn"),
    mobileTabbar: document.getElementById("mobileTabbar"),
    sidenavList: document.querySelector(".sidenav-list"),
  };
  return els;
}

// 初始化时立即调用一次，确保后续代码能直接用 els
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', getEls);
} else {
  getEls();
}

let bridgeReady = null;
let providerOptions = [];
let savingConfig = false;
let configLoaded = false;

// 数值字段默认值单表：加载回退与保存回退共用，防止两处漂移（复审 R2）
const DEFAULT_CONFIG = {
  decision_temperature: 0.2,
  decision_timeout_sec: 20,
  min_context_messages: 5,
  message_delay_sec: 60,
  min_silence_sec: 45,
  cooldown_sec: 900,
  vision_max_images: 2,
  vision_image_age_sec: 300,
  vision_timeout_sec: 20,
};

const PROMPT_PREVIEW_VALUES = {
  session: "aiocqhttp:GroupMessage:123456789",
  trigger: "message_delay",
  bot_aliases: "阿绪, 咕咕",
  latest_message: "这个问题有没有更稳一点的做法？",
  recent_messages: [
    "[小林] 我刚试了下，直接改参数好像会让回复变得太积极。",
    "[阿茶] 是不是应该先看最近几条消息有没有明确空位？",
    "[小林] 对，我担心它在别人聊天正热的时候插进来。",
    "[阿茶] 这个问题有没有更稳一点的做法？",
  ].join("\n"),
  last_message_age_sec: "65",
  last_reply_age_sec: "900",
};

const THEME_KEY = "selfreply-theme";
const THEME_CYCLE = ["auto", "light", "dark"];
// 三态循环对读屏用户需可感知：按钮 aria-label 同步当前状态（复审：主题切换无状态语义）
const THEME_LABELS = { auto: "跟随系统", light: "浅色 · 慈爱之惠", dark: "深色 · 审判之司" };

// P2-7: 具名时间常量——同一事件只改一处，不用追漏
const REFRESH_ARM_MS = 3000;    // 刷新按钮武装窗口（与下方 toast 文案「3 秒内」共享）
const TOAST_MS = 2200;          // toast 自动隐藏延迟
const SAVE_ANIM_MS = 1100;      // 保存按钮勾选动画清理
const SAVE_DOT_MS = 700;        // 导航状态点脉冲清理
const PREVIEW_DEBOUNCE_MS = 80; // 提示词预览防抖延迟

function currentTheme() {
  const value = document.documentElement.getAttribute("data-theme");
  return value === "light" || value === "dark" ? value : "auto";
}

function applyTheme(theme) {
  if (theme === "auto") {
    document.documentElement.removeAttribute("data-theme");
  } else {
    document.documentElement.setAttribute("data-theme", theme);
  }
  cacheThemeLocally(theme);
  if (els && els.themeToggle) {
    els.themeToggle.setAttribute("aria-label", `切换主题，当前：${THEME_LABELS[theme] || THEME_LABELS.auto}`);
  }
}

// AstrBot 插件页面以 iframe 嵌入 Dashboard，localStorage 不可用（访问即抛异常），
// 持久化必须走后端 ui/theme API；localStorage 仅作为直接打开页面时的缓存。
function cacheThemeLocally(theme) {
  try {
    if (theme === "auto") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, theme);
  } catch (error) {
    /* iframe 环境 localStorage 不可用，忽略 */
  }
}

async function persistTheme(theme) {
  cacheThemeLocally(theme);
  try {
    await apiPost("ui/theme", { theme });
  } catch (error) {
    /* 后端持久化失败仅当次生效 */
  }
}

async function restoreTheme() {
  // 后端是 iframe 环境下的权威主题源；localStorage 缓存仅作首帧防闪
  try {
    const result = await apiGet("ui/theme");
    const saved = result && result.ok !== false ? String(result.theme || "auto").trim() : "auto";
    const next = saved === "light" || saved === "dark" ? saved : "auto";
    if (next !== currentTheme()) applyTheme(next);
  } catch (error) {
    /* 后端不可用时保持当前（localStorage/系统）主题 */
  }
}

function cycleTheme() {
  const next = THEME_CYCLE[(THEME_CYCLE.indexOf(currentTheme()) + 1) % THEME_CYCLE.length];
  applyTheme(next);
  persistTheme(next);
}

function setStatState(element, state) {
  if (!element) return;
  element.classList.remove("is-on", "is-off", "is-info");
  element.classList.add(state);
}

// P2-6: 语义保存状态标志，避免用文案字符串做判断
// 状态映射: "" / "dirty" / "saving" / "ok" / "error"
//   dirty   + saving 共用 CSS is-pending
let saveStateKind = "";

function setSaveState(message, state) {
  saveStateKind = state;
  if (!els.configSaveState) return;
  els.configSaveState.textContent = message;
  els.configSaveState.classList.remove("is-pending", "is-ok", "is-error");
  // P2-6: dirty 和 saving 语义不同，但共用同一 CSS 颜色
  const cssKind = state === "dirty" ? "pending" : state;
  if (cssKind) els.configSaveState.classList.add(`is-${cssKind}`);
  // 同步导航底部保存状态（P2-5）
  if (els.navSaveState) {
    if (state === "ok") els.navSaveState.textContent = "已保存";
    else if (state === "error") els.navSaveState.textContent = "保存失败";
    else if (state === "saving") els.navSaveState.textContent = "保存中";
    // dirty 由 setDirty 统一设置导航文案，这里不重复
  }
  // 同步移动端固定保存条的状态
  if (els.mobileSaveState) {
    els.mobileSaveState.textContent = message || (state ? "" : "已同步");
    els.mobileSaveState.classList.remove("is-pending", "is-ok", "is-error");
    if (cssKind) els.mobileSaveState.classList.add(`is-${cssKind}`);
  }
  // 保存成功微反馈：三处保存按钮勾选回弹 + 导航状态点脉冲
  if (state === "ok") {
    const savedButtons = [
      els.saveTopBtn,
      els.saveMobileBtn,
      els.configForm ? els.configForm.querySelector('button[type="submit"]') : null,
    ];
    savedButtons.forEach((btn) => {
      if (!btn) return;
      btn.classList.remove("is-saved");
      void btn.offsetWidth; // 重置动画
      btn.classList.add("is-saved");
      window.setTimeout(() => btn.classList.remove("is-saved"), SAVE_ANIM_MS);
    });
    if (els.navSaveDot) {
      els.navSaveDot.classList.remove("is-pulse");
      void els.navSaveDot.offsetWidth;
      els.navSaveDot.classList.add("is-pulse");
      window.setTimeout(() => els.navSaveDot.classList.remove("is-pulse"), SAVE_DOT_MS);
    }
  }
}

let isDirty = false;

function setDirty(dirty = true) {
  isDirty = dirty;
  if (els.saveTopBtn) els.saveTopBtn.classList.toggle("is-dirty", isDirty);
  const bottomSave = els.configForm ? els.configForm.querySelector('.form-actions button[type="submit"]') : null;
  if (bottomSave) bottomSave.classList.toggle("is-dirty", isDirty);
  if (els.navSaveDot) els.navSaveDot.classList.toggle("is-dirty", isDirty);
  if (els.mobileSaveBar) els.mobileSaveBar.classList.toggle("is-dirty", isDirty);
  if (els.navSaveState) els.navSaveState.textContent = isDirty ? "有未保存改动" : "已同步";
  if (dirty && saveStateKind !== "saving") {
    setSaveState("有未保存改动", "dirty");
  } else if (!dirty && saveStateKind === "dirty") {
    setSaveState("", "");
  }
}

function attachDirtyListeners() {
  if (!els.configForm) return;
  els.configForm.addEventListener("change", () => setDirty(true));
  els.configForm.addEventListener("input", (e) => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") {
      setDirty(true);
    }
  });
}

function showToast(message) {
  if (!els.toast) return;
  els.toast.textContent = message;
  els.toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => els.toast.classList.remove("show"), TOAST_MS);
}

// 输入防抖：预览区每次按键全量重渲染，低频设备会卡（复审 P2-8）
function debounce(fn, delay) {
  let timer = null;
  return (...args) => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => fn(...args), delay);
  };
}

async function getBridge() {
  if (!window.AstrBotPluginPage) return null;
  if (!bridgeReady) {
    bridgeReady = window.AstrBotPluginPage.ready().catch(() => null);
  }
  await bridgeReady;
  return window.AstrBotPluginPage;
}

// P1-4: 统一 fetch 错误包装，提升后端不可达时的用户体验
function wrapFetchError(error) {
  const message = String(error?.message || error || "");
  if (message === "Failed to fetch" || message.includes("fetch")) {
    throw new Error("无法连接插件 API，请重载页面或重启 AstrBot 后重试");
  }
  throw error;
}

// P1-5: fetch 超时保护，防止后端挂起时请求永不 settle
const FETCH_TIMEOUT_MS = 15000;

async function apiGet(endpoint, params = {}) {
  try {
    const bridge = await getBridge();
    if (bridge) return bridge.apiGet(endpoint, params);
    const url = new URL(`/api/plug/${PLUGIN_ID}/${endpoint}`, window.location.href);
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, value);
    });
    const response = await fetch(url, {
      credentials: "include",
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(result?.error || `请求失败 (${response.status})`);
    }
    return result;
  } catch (error) {
    wrapFetchError(error);
  }
}

async function apiPost(endpoint, body = {}) {
  try {
    const bridge = await getBridge();
    if (bridge) return bridge.apiPost(endpoint, body);

    const url = new URL(`/api/plug/${PLUGIN_ID}/${endpoint}`, window.location.href);
    const response = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(result?.error || `请求失败 (${response.status})`);
    }
    return result;
  } catch (error) {
    wrapFetchError(error);
  }
}

function fmtBool(value) {
  return value ? "启用" : "关闭";
}

function num(value, fallback) {
  if (value === "" || value === undefined || value === null) return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function escapeHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function renderPromptTemplateHtml(template, values) {
  const escapedTemplate = escapeHtml(template);
  return escapedTemplate.replace(/\{([a-zA-Z0-9_]+)\}/g, (match, key) => {
    if (Object.prototype.hasOwnProperty.call(values, key)) {
      const val = escapeHtml(values[key]);
      return `<span class="prompt-var-tag" title="变量 {${key}}">${val}</span>`;
    }
    return match;
  });
}

function renderPromptPreview() {
  if (!els.promptPreview) return;
  const template = els.decisionPromptInput.value || els.decisionPromptInput.dataset.defaultPrompt || "";
  els.promptPreview.innerHTML = renderPromptTemplateHtml(template, PROMPT_PREVIEW_VALUES);
}

/**
 * 构造一个「下拉选择 + 手动输入」的 Provider 控件。
 *
 * 判断模型与两个识图 Provider 的控件结构完全一致，用同一工厂避免重复实现。
 * 控件自己持有 manual 状态，对外只暴露 value / render / sync / setManual；
 * onModeChange 回调用于联动周边 UI（如判断模型的提示文案与容器样式）。
 *
 * @param {{select: HTMLSelectElement|null, input: HTMLInputElement|null,
 *          button: HTMLButtonElement|null, placeholder: string}} refs
 * @param {(manual: boolean) => void} [onModeChange]
 */
function createProviderControl(refs, onModeChange) {
  let manual = false;

  function setManual(enabled) {
    manual = Boolean(enabled);
    if (refs.button) refs.button.textContent = manual ? "使用列表" : "手动输入";
    if (refs.select) refs.select.style.display = manual ? "none" : "block";
    if (refs.input) refs.input.style.display = manual ? "block" : "none";
    if (onModeChange) onModeChange(manual);
  }

  function value() {
    if (manual) return refs.input ? refs.input.value.trim() : "";
    return refs.select ? refs.select.value.trim() : "";
  }

  function render() {
    if (!refs.select) return;
    const current = refs.select.value;
    refs.select.innerHTML = "";
    const fallback = document.createElement("option");
    fallback.value = "";
    fallback.textContent = refs.placeholder;
    refs.select.appendChild(fallback);
    providerOptions.forEach((provider) => {
      const option = document.createElement("option");
      option.value = provider.id;
      option.textContent = provider.label || provider.id;
      refs.select.appendChild(option);
    });
    refs.select.value = current;
  }

  function sync(providerId) {
    const next = String(providerId || "").trim();
    const known = next === "" || providerOptions.some((provider) => provider.id === next);
    if (known && refs.select) {
      refs.select.value = next;
      if (refs.input) refs.input.value = "";
      setManual(false);
      return;
    }
    if (refs.input) refs.input.value = next;
    setManual(true);
  }

  if (refs.button) {
    refs.button.addEventListener("click", () => {
      if (manual) {
        sync(refs.input ? refs.input.value.trim() : "");
        if (manual) showToast("当前 Provider 不在列表中，继续保留手动输入");
        return;
      }
      if (refs.input) refs.input.value = refs.select ? refs.select.value || "" : "";
      setManual(true);
    });
  }
  if (refs.select) {
    refs.select.addEventListener("change", () => {
      if (refs.input) refs.input.value = "";
    });
  }

  return { value, render, sync, setManual };
}

const visionProviderControl = createProviderControl({
  select: els.visionProviderSelect,
  input: els.visionProviderInput,
  button: els.visionProviderManualBtn,
  placeholder: "使用当前会话模型",
});

// 留空 = 与主识图 Provider 一致，回落逻辑由后端 Settings 统一处理
const visionJudgeProviderControl = createProviderControl({
  select: els.visionJudgeProviderSelect,
  input: els.visionJudgeProviderInput,
  button: els.visionJudgeProviderManualBtn,
  placeholder: "与识图模型一致",
});

// 判断模型 Provider：与识图控件同工厂（复审 R1），onModeChange 联动周边提示
const judgeProviderControl = createProviderControl(
  {
    select: els.judgeProviderSelect,
    input: els.judgeProviderInput,
    button: els.providerManualBtn,
    placeholder: "使用当前会话默认模型",
  },
  (manual) => {
    if (els.providerField) els.providerField.classList.toggle("manual", manual);
    if (els.providerHint) {
      els.providerHint.textContent = manual
        ? "手动输入为空时使用当前会话默认模型"
        : "留空表示使用当前会话默认模型";
    }
  }
);

async function loadProviders() {
  try {
    const result = await apiGet("providers");
    if (!result || result.ok === false) {
      throw new Error(result?.error || "无法加载 Provider 列表");
    }
    providerOptions = Array.isArray(result.providers)
      ? result.providers.filter((item) => item && item.id)
      : [];
    judgeProviderControl.render();
    visionProviderControl.render();
    visionJudgeProviderControl.render();
  } catch (error) {
    providerOptions = [];
    judgeProviderControl.render();
    visionProviderControl.render();
    visionJudgeProviderControl.render();
    judgeProviderControl.setManual(true);
    showToast("无法加载 Provider 列表，可手动填写");
  }
}

async function loadConfig() {
  const config = await apiGet("config");
  // 后端异常时返回 {ok: false, error}，此时不得用假默认值填表单（防止
  // 用户保存时把未加载的默认值写回真实配置）。
  if (!config || config.ok === false) {
    throw new Error(config?.error || "配置加载失败");
  }
  els.enabledInput.checked = Boolean(config.enabled);
  els.decisionModelInput.checked = config.decision_model_enabled !== false;
  judgeProviderControl.sync(config.judge_provider_id || "");
  els.decisionTempInput.value =
    config.decision_temperature ?? DEFAULT_CONFIG.decision_temperature;
  els.decisionTimeoutInput.value =
    config.decision_timeout_sec ?? DEFAULT_CONFIG.decision_timeout_sec;
  els.decisionPromptInput.value = config.decision_prompt_template || config.decision_prompt_default || "";
  els.decisionPromptInput.dataset.defaultPrompt = config.decision_prompt_default || config.decision_prompt_template || "";
  els.minContextInput.value =
    config.min_context_messages ?? config.proactive_threshold ?? DEFAULT_CONFIG.min_context_messages;
  els.messageDelayInput.value =
    config.message_delay_sec ?? config.idle_trigger_seconds ?? DEFAULT_CONFIG.message_delay_sec;
  els.minSilenceInput.value = config.min_silence_sec ?? DEFAULT_CONFIG.min_silence_sec;
  els.cooldownInput.value =
    config.cooldown_sec ?? config.cooldown_seconds ?? DEFAULT_CONFIG.cooldown_sec;
  els.visionJudgeEnabledInput.checked = Boolean(config.vision_judge_enabled);
  els.visionMainEnabledInput.checked = Boolean(config.vision_main_enabled);
  els.visionSkipStickersInput.checked = Boolean(config.vision_skip_stickers);
  visionProviderControl.sync(config.vision_provider_id || "");
  visionJudgeProviderControl.sync(config.vision_judge_provider_id || "");
  els.visionMaxImagesInput.value = config.vision_max_images ?? DEFAULT_CONFIG.vision_max_images;
  els.visionImageAgeInput.value = config.vision_image_age_sec ?? DEFAULT_CONFIG.vision_image_age_sec;
  els.visionTimeoutInput.value = config.vision_timeout_sec ?? DEFAULT_CONFIG.vision_timeout_sec;
  els.proactiveInheritToolsInput.checked = Boolean(config.proactive_inherit_tools);
  const whitelist = Array.isArray(config.whitelist) ? config.whitelist : [];
  els.whitelistInput.value = whitelist.join("\n");
  els.whitelistCount.textContent = String(whitelist.length);
  if (els.decisionModelStatus) {
    const decisionOn = config.decision_model_enabled !== false;
    els.decisionModelStatus.textContent = fmtBool(decisionOn);
    setStatState(els.decisionModelStat, decisionOn ? "is-on" : "is-off");
  }
  renderPromptPreview();
  // 状态点三态：持久关闭 / 持久开启但 /off 暂停 / 运行中。开关 checked 保持持久值，
  // 避免全量保存把临时暂停固化成永久关闭（与后端 enabled/runtime_enabled 契约一致）。
  const runtimeOn = config.runtime_enabled !== false;
  els.selfStatus.textContent = config.enabled
    ? (runtimeOn ? "启用" : "已暂停（/off）")
    : "关闭";
  // 状态染色与文案同源（config 端点），避免与 overview 端点并发互相覆盖。
  setStatState(els.selfStat, runtimeOn ? "is-on" : "is-off");
  configLoaded = true;
  setDirty(false);
}

// --------------------------------------------------------------------------
// 字段内联校验
//   收集所有带 min/max 的数字输入，超出范围时就地标红 + 友好提示，
//   并在保存前阻止非法提交，自动聚焦第一个问题字段。
// --------------------------------------------------------------------------

const numberFields = [];

function setupValidation() {
  if (!els.configForm) return;
  const inputs = els.configForm.querySelectorAll('input[type="number"]');
  inputs.forEach((input) => {
    if (!input.hasAttribute("min") && !input.hasAttribute("max")) return;
    const min = input.hasAttribute("min") ? Number(input.getAttribute("min")) : null;
    const max = input.hasAttribute("max") ? Number(input.getAttribute("max")) : null;
    const error = document.createElement("span");
    error.className = "field-error";
    error.setAttribute("role", "alert");
    input.insertAdjacentElement("afterend", error);
    const entry = { input, error, min, max };
    numberFields.push(entry);
    const handler = () => validateField(entry);
    input.addEventListener("input", handler);
    input.addEventListener("blur", handler);
  });
}

function validateField(entry) {
  const { input, error, min, max } = entry;
  const raw = input.value.trim();
  let msg = "";
  if (raw !== "") {
    const val = Number(raw);
    if (!Number.isFinite(val)) {
      msg = "请输入有效的数字";
    } else if (min !== null && val < min) {
      msg = `不能小于 ${min}`;
    } else if (max !== null && val > max) {
      msg = `不能大于 ${max}`;
    }
  }
  if (msg) {
    input.setAttribute("aria-invalid", "true");
    error.textContent = msg;
    error.classList.add("show");
    return false;
  }
  input.removeAttribute("aria-invalid");
  error.classList.remove("show");
  error.textContent = "";
  return true;
}

function validateAll() {
  let firstInvalid = null;
  let ok = true;
  numberFields.forEach((entry) => {
    const valid = validateField(entry);
    if (!valid) {
      ok = false;
      if (!firstInvalid) firstInvalid = entry.input;
    }
  });
  if (firstInvalid) {
    const details = firstInvalid.closest("details");
    if (details && !details.open) details.open = true;
    firstInvalid.scrollIntoView({ block: "center", behavior: prefersReducedMotion() ? "auto" : "smooth" });
    firstInvalid.focus({ preventScroll: true });
  }
  return ok;
}

// P2-10: 白名单前端预校验——与后端 _string_list 规则对齐（len > 200 / 非法字符）
const WHITELIST_ITEM_MAX_LEN = 200;             // 与后端 MAX_STRING_LIST_ITEM_LEN 保持同步
const WHITELIST_ILLEGAL_RE = /[\x00-\x1f"'\\]/; // 与后端 re.search(r'[\x00-\x1f"\'\\]') 同步

function validateWhitelist() {
  if (!els.whitelistInput || !els.whitelistError) return true;
  const items = els.whitelistInput.value
    .split(/[\n,，]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  let msg = "";
  for (const item of items) {
    if (item.length > WHITELIST_ITEM_MAX_LEN) {
      msg = `有条目过长（> ${WHITELIST_ITEM_MAX_LEN} 字符）：「${item.slice(0, 24)}…」`;
      break;
    }
    if (WHITELIST_ILLEGAL_RE.test(item)) {
      msg = `有条目包含非法字符（不允许引号或反斜杠）：「${item.slice(0, 24)}」`;
      break;
    }
  }
  if (msg) {
    els.whitelistInput.setAttribute("aria-invalid", "true");
    els.whitelistError.textContent = msg;
    els.whitelistError.classList.add("show");
    els.whitelistInput.scrollIntoView({ block: "center", behavior: prefersReducedMotion() ? "auto" : "smooth" });
    els.whitelistInput.focus({ preventScroll: true });
    return false;
  }
  els.whitelistInput.removeAttribute("aria-invalid");
  els.whitelistError.classList.remove("show");
  els.whitelistError.textContent = "";
  return true;
}

function prefersReducedMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

// --------------------------------------------------------------------------
// 导航 · 滚动高亮（scroll-spy）+ 平滑跳转
// --------------------------------------------------------------------------

function setupNav() {
  const links = Array.from(document.querySelectorAll(".sidenav-link"));
  if (!links.length) return;
  const byTarget = new Map(links.map((link) => [link.dataset.target, link]));

  links.forEach((link) => {
    link.addEventListener("click", (e) => {
      const target = document.getElementById(link.dataset.target);
      if (!target) return;
      e.preventDefault();
      const details = target.closest("details");
      if (details && !details.open) details.open = true;
      target.scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start" });
      try { history.replaceState(null, "", "#" + link.dataset.target); } catch (_) { /* 忽略 */ }
      setCurrentNav(link);
    });
  });

  if ("IntersectionObserver" in window) {
    const sections = links.map((l) => document.getElementById(l.dataset.target)).filter(Boolean);
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          const link = byTarget.get(entry.target.id);
          if (link) setCurrentNav(link);
        }
      });
    }, { rootMargin: "-28% 0px -62% 0px", threshold: 0 });
    sections.forEach((s) => observer.observe(s));
  }

  setCurrentNav(links[0]);
}

function setCurrentNav(active) {
  document.querySelectorAll(".sidenav-link").forEach((link) => {
    const on = link === active;
    link.classList.toggle("is-current", on);
    if (on) link.setAttribute("aria-current", "true");
    else link.removeAttribute("aria-current");
  });
  updateNavFades();
  // 仅 ≤1024px 横向导航条需要把当前项滚入可视区。
  // 两个坑都在这里踩过（实测复现）：
  //   1. 不能用 scrollIntoView——它会取消同一文档里正在进行的主平滑滚动，
  //      导致点击分区后页面原地不动（桌面端 2px 容差会被边框宽度误触发）；
  //   2. 不能只看「侧栏可见」——桌面端侧栏恒可见，必须用断点判定横向模式。
  // 直接滚动列表容器的 scrollLeft，不触碰页面滚动。
  if (active && els.sidenavList && window.matchMedia("(max-width: 1024px)").matches) {
    const linkRect = active.getBoundingClientRect();
    const listRect = els.sidenavList.getBoundingClientRect();
    if (linkRect.left < listRect.left + 2 || linkRect.right > listRect.right - 2) {
      const delta = linkRect.left - listRect.left - (listRect.width - linkRect.width) / 2;
      els.sidenavList.scrollTo({
        left: els.sidenavList.scrollLeft + delta,
        behavior: prefersReducedMotion() ? "auto" : "smooth",
      });
    }
  }
  // 同步移动端底部 Tab 当前态
  syncMobileTabs(active);
}

// 分区 → 底部 Tab 分组映射（高级组含运行边界与图片识别）
const TAB_GROUPS = {
  selfStat: "selfStat",
  "sec-scope": "sec-scope",
  "sec-triggers": "sec-scope",
  "sec-decision": "sec-decision",
  "sec-runtime": "sec-runtime",
  "sec-vision": "sec-runtime",
};

function syncMobileTabs(active) {
  if (!els.mobileTabbar || !active) return;
  const group = TAB_GROUPS[active.dataset.target] || active.dataset.target;
  els.mobileTabbar.querySelectorAll(".mtab").forEach((tab) => {
    tab.classList.toggle("is-current", tab.dataset.target === group);
  });
}

// 按横向滚动位置切换左右渐隐提示
function updateNavFades() {
  if (!els.sidenavList) return;
  const list = els.sidenavList;
  const startFade = document.querySelector(".sidenav-fade-start");
  const endFade = document.querySelector(".sidenav-fade-end");
  if (startFade) startFade.classList.toggle("is-hidden", list.scrollLeft <= 4);
  if (endFade) {
    const atEnd = list.scrollLeft + list.clientWidth >= list.scrollWidth - 4;
    endFade.classList.toggle("is-hidden", atEnd);
  }
}

// --------------------------------------------------------------------------
// 滚动进度条 + 顶栏附着态
// --------------------------------------------------------------------------

function updateScrollProgress() {
  if (!els.scrollProgress) return;
  const doc = document.documentElement;
  const scrollTop = doc.scrollTop || document.body.scrollTop || window.scrollY || 0;
  const height = doc.scrollHeight - doc.clientHeight;
  const pct = height > 0 ? Math.min(100, Math.max(0, (scrollTop / height) * 100)) : 0;
  els.scrollProgress.style.setProperty("--progress", pct + "%");
}

function updateTopbarStuck() {
  if (!els.topbar) return;
  const y = window.scrollY || document.documentElement.scrollTop || 0;
  els.topbar.classList.toggle("is-stuck", y > 8);
}

let scrollTicking = false;
function onScroll() {
  if (scrollTicking) return;
  scrollTicking = true;
  requestAnimationFrame(() => {
    updateScrollProgress();
    updateTopbarStuck();
    scrollTicking = false;
  });
}

// 保存按钮 loading 态（顶部与底部两处同步）
function setSaving(loading) {
  const buttons = [
    els.saveTopBtn,
    els.saveMobileBtn,
    els.configForm ? els.configForm.querySelector('button[type="submit"]') : null,
  ];
  buttons.forEach((btn) => {
    if (!btn) return;
    btn.classList.toggle("is-loading", loading);
    btn.disabled = loading;
  });
  // P1-1: 保存期间禁用刷新按钮，避免并发竞态
  if (els.refreshBtn) els.refreshBtn.disabled = loading;
}

async function saveConfig(event) {
  event.preventDefault();
  if (savingConfig) {
    showToast("正在保存…");
    return;
  }
  if (!configLoaded) {
    showToast("配置尚未成功加载，请先刷新页面");
    return;
  }
  // 保存前先校验数值字段，避免把越界值写回后端
  if (!validateAll()) {
    showToast("部分数值超出允许范围，请检查标红字段");
    return;
  }
  // P2-10: 白名单前端预校验
  if (!validateWhitelist()) {
    showToast("白名单有非法条目，请检查标红区域");
    return;
  }
  savingConfig = true;
  setSaving(true);
  els.configForm.inert = true; // 保存期间禁编辑，防止 reload 冲掉新输入
  els.configForm.classList.add("is-saving"); // 视觉反馈：inert 不可编辑需可感知（复审 P2-10）
  setSaveState("保存中", "saving");
  try {
    const whitelist = els.whitelistInput.value
      .split(/[\n,，]+/)
      .map((item) => item.trim())
      .filter(Boolean);
    const result = await apiPost("config", {
      enabled: els.enabledInput.checked,
      decision_model_enabled: els.decisionModelInput.checked,
      judge_provider_id: judgeProviderControl.value(),
      decision_temperature: num(els.decisionTempInput.value, DEFAULT_CONFIG.decision_temperature),
      decision_timeout_sec: num(els.decisionTimeoutInput.value, DEFAULT_CONFIG.decision_timeout_sec),
      decision_prompt_template: els.decisionPromptInput.value.trim(),
      min_context_messages: num(els.minContextInput.value, DEFAULT_CONFIG.min_context_messages),
      message_delay_sec: num(els.messageDelayInput.value, DEFAULT_CONFIG.message_delay_sec),
      min_silence_sec: num(els.minSilenceInput.value, DEFAULT_CONFIG.min_silence_sec),
      cooldown_sec: num(els.cooldownInput.value, DEFAULT_CONFIG.cooldown_sec),
      vision_judge_enabled: els.visionJudgeEnabledInput.checked,
      vision_main_enabled: els.visionMainEnabledInput.checked,
      vision_skip_stickers: els.visionSkipStickersInput.checked,
      vision_provider_id: visionProviderControl.value(),
      vision_judge_provider_id: visionJudgeProviderControl.value(),
      vision_max_images: num(els.visionMaxImagesInput.value, DEFAULT_CONFIG.vision_max_images),
      vision_image_age_sec: num(els.visionImageAgeInput.value, DEFAULT_CONFIG.vision_image_age_sec),
      vision_timeout_sec: num(els.visionTimeoutInput.value, DEFAULT_CONFIG.vision_timeout_sec),
      proactive_inherit_tools: els.proactiveInheritToolsInput.checked,
      whitelist,
    });
    if (!result || result.ok !== true) {
      setSaveState("保存失败", "error");
      showToast(result?.error || "保存失败");
      return;
    }
    setSaveState("已保存", "ok");
    setDirty(false);
    els.whitelistCount.textContent = String(whitelist.length);
    showToast("配置已保存");
    // 保存成功即定案；刷新显示失败不再回写"保存失败"（已落盘，避免误导重存）
    try {
      await loadOverview();
      await loadConfig();
    } catch (error) {
      showToast("已保存，但刷新显示失败，请点刷新");
    }
  } finally {
    savingConfig = false;
    els.configForm.classList.remove("is-saving");
    els.configForm.inert = false;
    setSaving(false);
  }
}

async function cleanupImageCache() {
  if (!els.cleanupImageCacheBtn) return;
  els.cleanupImageCacheBtn.disabled = true;
  if (els.cleanupImageCacheState) els.cleanupImageCacheState.textContent = "清理中…";
  try {
    const result = await apiPost("image-cache/cleanup");
    if (!result || result.ok !== true) {
      throw new Error(result?.error || "图片缓存清理失败");
    }
    const removed = Number(result.removed || 0);
    if (els.cleanupImageCacheState) {
      els.cleanupImageCacheState.textContent = removed
        ? `已清理 ${removed} 个过期图片`
        : "没有需要清理的过期图片";
    }
    showToast(removed ? `已清理 ${removed} 个过期图片` : "没有需要清理的过期图片");
  } catch (error) {
    if (els.cleanupImageCacheState) els.cleanupImageCacheState.textContent = "清理失败";
    showToast(error.message || "图片缓存清理失败");
  } finally {
    els.cleanupImageCacheBtn.disabled = false;
  }
}

async function loadOverview() {
  const overview = await apiGet("unified/overview");
  const self = overview.self_reply || {};
  // 状态点文案与染色均由 loadConfig 统一管理（config 端点同时含持久/运行时两态）；
  // 这里只刷新会话计数。
  els.whitelistCount.textContent = String(self.whitelist_count || 0);
}

async function loadAll() {
  await loadProviders();
  await Promise.all([loadConfig(), loadOverview()]);
}

// 刷新：iframe 沙箱里 window.confirm 可能不弹窗直接返回 false（点击看似无反应），
// 改用「双击确认」模式：有未保存改动时第一次点击进入 3 秒待确认态，再点才真刷新。
let refreshing = false;
let refreshArmed = false;
let refreshArmTimer = null;

async function doRefresh() {
  // P1-1: 保存进行中不允许刷新（双保险）
  if (savingConfig || refreshing) return;
  refreshing = true;
  els.refreshBtn.disabled = true;
  els.refreshBtn.classList.add("is-loading");
  try {
    await loadAll();
    showToast("已刷新为最新配置");
  } catch (err) {
    showToast(err.message || "刷新失败");
  } finally {
    refreshing = false;
    els.refreshBtn.disabled = false;
    els.refreshBtn.classList.remove("is-loading");
  }
}

if (els.refreshBtn) {
  els.refreshBtn.addEventListener("click", () => {
    if (refreshing) return;
    if (isDirty && !refreshArmed) {
      refreshArmed = true;
      els.refreshBtn.classList.add("is-armed");
      showToast("有未保存改动，3 秒内再点一次刷新将丢弃改动");
      window.clearTimeout(refreshArmTimer);
      refreshArmTimer = window.setTimeout(() => {
        refreshArmed = false;
        els.refreshBtn.classList.remove("is-armed");
      }, REFRESH_ARM_MS);
      return;
    }
    refreshArmed = false;
    window.clearTimeout(refreshArmTimer);
    els.refreshBtn.classList.remove("is-armed");
    doRefresh();
  });
}
if (els.cleanupImageCacheBtn) {
  els.cleanupImageCacheBtn.addEventListener("click", () => cleanupImageCache());
}
// 恢复默认提示词：点击即生效（写入默认提示词并标记待保存），不再弹二次确认条
if (els.resetPromptBtn) {
  els.resetPromptBtn.addEventListener("click", () => {
    // 配置未成功加载时没有默认提示词来源，此时清空会丢用户输入，直接拦截
    if (!configLoaded) {
      showToast("配置尚未成功加载，请先刷新页面");
      return;
    }
    els.decisionPromptInput.value = els.decisionPromptInput.dataset.defaultPrompt || "";
    renderPromptPreview();
    setDirty(true);
    showToast("已恢复默认提示词，点击保存后生效");
  });
}
if (els.decisionPromptInput) {
  els.decisionPromptInput.addEventListener("input", debounce(renderPromptPreview, PREVIEW_DEBOUNCE_MS));
}
// 本地开关即时反馈：未保存前文案标记「（未保存）」，保存后由 loadConfig 以服务端态覆盖
if (els.enabledInput) {
  els.enabledInput.addEventListener("change", () => {
    els.selfStatus.textContent = els.enabledInput.checked ? "启用（未保存）" : "关闭（未保存）";
    setDirty(true);
  });
}
if (els.decisionModelInput) {
  els.decisionModelInput.addEventListener("change", () => {
    const on = els.decisionModelInput.checked;
    els.decisionModelStatus.textContent = fmtBool(on);
    setStatState(els.decisionModelStat, on ? "is-on" : "is-off");
    setDirty(true);
  });
}
if (els.configForm) {
  els.configForm.addEventListener("submit", (event) => saveConfig(event).catch((err) => {
    setSaveState("保存失败", "error");
    showToast(err.message || "保存失败");
  }));
}

// 主题切换：跟随系统 → 浅色 → 深色
if (els.themeToggle) {
  els.themeToggle.addEventListener("click", cycleTheme);
}

// 页面加载时恢复保存的主题
try {
  const saved = localStorage.getItem(THEME_KEY);
  if (saved === "light" || saved === "dark") {
    applyTheme(saved);
  }
} catch (error) {
  /* localStorage 不可用时使用默认 */
}

// 顶部保存按钮
if (els.saveTopBtn) {
  els.saveTopBtn.addEventListener("click", () => {
    els.configForm.requestSubmit();
  });
}

// 导航 / 校验 / 滚动反馈初始化（不依赖后端，先就绪）
setupNav();
setupValidation();
window.addEventListener("scroll", onScroll, { passive: true });
updateScrollProgress();
updateTopbarStuck();

attachDirtyListeners();

// 移动端横向导航渐隐提示：随滚动 / 窗口尺寸更新
if (els.sidenavList) {
  els.sidenavList.addEventListener("scroll", updateNavFades, { passive: true });
  window.addEventListener("resize", updateNavFades, { passive: true });
}

// 移动端固定保存条：复用同一套保存流程
if (els.saveMobileBtn) {
  els.saveMobileBtn.addEventListener("click", () => {
    els.configForm.requestSubmit();
  });
}

// 移动端底部 Tab 导航：点击平滑滚动到对应分区，并开合所属折叠组
if (els.mobileTabbar) {
  els.mobileTabbar.querySelectorAll(".mtab").forEach((tab) => {
    tab.addEventListener("click", () => {
      const target = document.getElementById(tab.dataset.target);
      if (!target) return;
      const details = target.closest("details");
      if (details && !details.open) details.open = true;
      target.scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start" });
      const link = document.querySelector('.sidenav-link[data-target="' + tab.dataset.target + '"]');
      if (link) setCurrentNav(link);
    });
  });
}

window.addEventListener("beforeunload", (e) => {
  if (isDirty) {
    e.preventDefault();
    e.returnValue = "";
  }
});

function hideBoot() {
  if (els.boot) els.boot.classList.add("is-hidden");
  document.body.classList.add("is-ready");
  // 触发总开关 Hero 入场（双保险：body.is-ready 也会兜底显示）
  if (els.selfStat) els.selfStat.classList.add("is-entered");
}

// P1-5: boot 遗罩兜底定时器，防止后端挂起时页面永久卡死
const BOOT_TIMEOUT_MS = 12000;
const bootTimeout = window.setTimeout(() => {
  hideBoot();
  showToast("加载超时，请刷新页面或检查后端状态");
}, BOOT_TIMEOUT_MS);

loadAll()
  .then(() => {
    window.clearTimeout(bootTimeout);
    hideBoot();
  })
  .catch((err) => {
    window.clearTimeout(bootTimeout);
    hideBoot();
    showToast(err.message || "加载失败");
  });
// 主题权威源在后端（iframe 下 localStorage 不可用），加载完成后异步恢复
restoreTheme();
