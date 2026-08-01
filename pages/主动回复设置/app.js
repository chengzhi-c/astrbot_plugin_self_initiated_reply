const PLUGIN_ID = "astrbot_plugin_self_initiated_reply";

const els = {
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
  providerField: document.querySelector(".provider-field"),
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
  whitelistInput: document.getElementById("whitelistInput"),
  configSaveState: document.getElementById("configSaveState"),
  toast: document.getElementById("toast"),
};

let bridgeReady = null;
let providerOptions = [];
let providerManualMode = false;

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
  try {
    if (theme === "auto") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, theme);
  } catch (error) {
    /* localStorage 不可用时仅当次生效 */
  }
}

function cycleTheme() {
  const next = THEME_CYCLE[(THEME_CYCLE.indexOf(currentTheme()) + 1) % THEME_CYCLE.length];
  applyTheme(next);
}

function setStatState(element, state) {
  if (!element) return;
  element.classList.remove("is-on", "is-off", "is-info");
  element.classList.add(state);
}

function setSaveState(text, kind) {
  if (!els.configSaveState) return;
  els.configSaveState.textContent = text || "";
  els.configSaveState.classList.remove("is-pending", "is-ok", "is-error");
  if (kind) els.configSaveState.classList.add(`is-${kind}`);
}

function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => els.toast.classList.remove("show"), 2200);
}

async function getBridge() {
  if (!window.AstrBotPluginPage) return null;
  if (!bridgeReady) {
    bridgeReady = window.AstrBotPluginPage.ready().catch(() => null);
  }
  await bridgeReady;
  return window.AstrBotPluginPage;
}

async function apiGet(endpoint, params = {}) {
  const bridge = await getBridge();
  if (bridge) return bridge.apiGet(endpoint, params);
  const url = new URL(`/api/plug/${PLUGIN_ID}/${endpoint}`, window.location.href);
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, value);
  });
  const response = await fetch(url, { credentials: "include" });
  return response.json();
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
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(result?.error || `请求失败 (${response.status})`);
    }
    return result;
  } catch (error) {
    const message = String(error?.message || error || "");
    if (message === "Failed to fetch" || message.includes("fetch")) {
      throw new Error("无法连接插件 API，请重载页面或重启 AstrBot 后重试");
    }
    throw error;
  }
}

function fmtBool(value) {
  return value ? "启用" : "关闭";
}

function renderPromptTemplate(template, values) {
  return String(template || "").replace(/\{([a-zA-Z0-9_]+)\}/g, (match, key) => {
    if (Object.prototype.hasOwnProperty.call(values, key)) {
      return values[key];
    }
    return match;
  });
}

function renderPromptPreview() {
  if (!els.promptPreview) return;
  const template = els.decisionPromptInput.value || els.decisionPromptInput.dataset.defaultPrompt || "";
  els.promptPreview.textContent = renderPromptTemplate(template, PROMPT_PREVIEW_VALUES);
}

function setProviderManualMode(enabled) {
  providerManualMode = Boolean(enabled);
  if (els.providerField) {
    els.providerField.classList.toggle("manual", providerManualMode);
  }
  if (els.providerManualBtn) {
    els.providerManualBtn.textContent = providerManualMode ? "使用列表" : "手动输入";
  }
  if (els.providerHint) {
    els.providerHint.textContent = providerManualMode
      ? "手动输入为空时使用当前会话默认模型"
      : "留空表示使用当前会话默认模型";
  }
}

/**
 * 构造一个「下拉选择 + 手动输入」的 Provider 控件。
 *
 * 主识图与判断阶段识图的控件结构完全一致，用同一工厂避免重复实现。
 * 控件自己持有 manual 状态，对外只暴露 value / render / sync。
 *
 * @param {{select: HTMLSelectElement|null, input: HTMLInputElement|null,
 *          button: HTMLButtonElement|null, placeholder: string}} refs
 */
function createProviderControl(refs) {
  let manual = false;

  function setManual(enabled) {
    manual = Boolean(enabled);
    if (refs.button) refs.button.textContent = manual ? "使用列表" : "手动输入";
    if (refs.select) refs.select.style.display = manual ? "none" : "block";
    if (refs.input) refs.input.style.display = manual ? "block" : "none";
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

function currentProviderId() {
  if (providerManualMode) return els.judgeProviderInput.value.trim();
  return els.judgeProviderSelect ? els.judgeProviderSelect.value.trim() : els.judgeProviderInput.value.trim();
}

function renderProviderSelect() {
  if (!els.judgeProviderSelect) return;
  const current = els.judgeProviderSelect.value;
  els.judgeProviderSelect.innerHTML = "";
  const defaultOption = document.createElement("option");
  defaultOption.value = "";
  defaultOption.textContent = "使用当前会话默认模型";
  els.judgeProviderSelect.appendChild(defaultOption);
  providerOptions.forEach((provider) => {
    const option = document.createElement("option");
    option.value = provider.id;
    option.textContent = provider.label || provider.id;
    els.judgeProviderSelect.appendChild(option);
  });
  els.judgeProviderSelect.value = current;
}

function syncProviderControl(providerId) {
  const value = String(providerId || "").trim();
  const known = value === "" || providerOptions.some((provider) => provider.id === value);
  if (known && els.judgeProviderSelect) {
    els.judgeProviderSelect.value = value;
    els.judgeProviderInput.value = "";
    setProviderManualMode(false);
    return;
  }
  els.judgeProviderInput.value = value;
  setProviderManualMode(true);
}

async function loadProviders() {
  try {
    const result = await apiGet("providers");
    if (!result || result.ok === false) {
      throw new Error(result?.error || "无法加载 Provider 列表");
    }
    providerOptions = Array.isArray(result.providers)
      ? result.providers.filter((item) => item && item.id)
      : [];
    renderProviderSelect();
    visionProviderControl.render();
    visionJudgeProviderControl.render();
  } catch (error) {
    providerOptions = [];
    renderProviderSelect();
    visionProviderControl.render();
    visionJudgeProviderControl.render();
    setProviderManualMode(true);
    showToast("无法加载 Provider 列表，可手动填写");
  }
}

async function loadConfig() {
  const config = await apiGet("config");
  els.enabledInput.checked = Boolean(config.enabled);
  els.decisionModelInput.checked = config.decision_model_enabled !== false;
  syncProviderControl(config.judge_provider_id || "");
  els.decisionTempInput.value = config.decision_temperature ?? 0.2;
  els.decisionTimeoutInput.value = config.decision_timeout_sec ?? 20;
  els.decisionPromptInput.value = config.decision_prompt_template || config.decision_prompt_default || "";
  els.decisionPromptInput.dataset.defaultPrompt = config.decision_prompt_default || config.decision_prompt_template || "";
  els.minContextInput.value = config.min_context_messages ?? config.proactive_threshold ?? 5;
  els.messageDelayInput.value = config.message_delay_sec ?? config.idle_trigger_seconds ?? 60;
  els.minSilenceInput.value = config.min_silence_sec ?? 45;
  els.cooldownInput.value = config.cooldown_sec ?? config.cooldown_seconds ?? 900;
  els.visionJudgeEnabledInput.checked = Boolean(config.vision_judge_enabled);
  els.visionMainEnabledInput.checked = Boolean(config.vision_main_enabled);
  els.visionSkipStickersInput.checked = Boolean(config.vision_skip_stickers);
  visionProviderControl.sync(config.vision_provider_id || "");
  visionJudgeProviderControl.sync(config.vision_judge_provider_id || "");
  els.visionMaxImagesInput.value = config.vision_max_images ?? 2;
  els.visionImageAgeInput.value = config.vision_image_age_sec ?? 300;
  els.visionTimeoutInput.value = config.vision_timeout_sec ?? 20;
  const whitelist = Array.isArray(config.whitelist) ? config.whitelist : [];
  els.whitelistInput.value = whitelist.join("\n");
  els.whitelistCount.textContent = String(whitelist.length);
  if (els.decisionModelStatus) {
    const decisionOn = config.decision_model_enabled !== false;
    els.decisionModelStatus.textContent = fmtBool(decisionOn);
    setStatState(els.decisionModelStat, decisionOn ? "is-on" : "is-off");
  }
  renderPromptPreview();
}

async function saveConfig(event) {
  event.preventDefault();
  setSaveState("保存中", "pending");
  const whitelist = els.whitelistInput.value
    .split(/[\n,，]+/)
    .map((item) => item.trim())
    .filter(Boolean);
  const result = await apiPost("config", {
    enabled: els.enabledInput.checked,
    decision_model_enabled: els.decisionModelInput.checked,
    judge_provider_id: currentProviderId(),
    decision_temperature: Number(els.decisionTempInput.value || 0.2),
    decision_timeout_sec: Number(els.decisionTimeoutInput.value || 20),
    decision_prompt_template: els.decisionPromptInput.value.trim(),
    min_context_messages: Number(els.minContextInput.value || 5),
    message_delay_sec: Number(els.messageDelayInput.value || 60),
    min_silence_sec: Number(els.minSilenceInput.value || 45),
    cooldown_sec: Number(els.cooldownInput.value || 900),
    vision_judge_enabled: els.visionJudgeEnabledInput.checked,
    vision_main_enabled: els.visionMainEnabledInput.checked,
    vision_skip_stickers: els.visionSkipStickersInput.checked,
    vision_provider_id: visionProviderControl.value(),
    vision_judge_provider_id: visionJudgeProviderControl.value(),
    vision_max_images: Number(els.visionMaxImagesInput.value || 2),
    vision_image_age_sec: Number(els.visionImageAgeInput.value || 300),
    vision_timeout_sec: Number(els.visionTimeoutInput.value || 20),
    whitelist,
  });
  if (!result || result.ok !== true) {
    setSaveState("保存失败", "error");
    showToast(result?.error || "保存失败");
    return;
  }
  setSaveState("已保存", "ok");
  els.whitelistCount.textContent = String(whitelist.length);
  showToast("配置已保存");
  await loadOverview();
  await loadConfig();
}

async function loadOverview() {
  const overview = await apiGet("unified/overview");
  const self = overview.self_reply || {};
  const enabled = Boolean(self.enabled);
  els.selfStatus.textContent = fmtBool(enabled);
  setStatState(els.selfStat, enabled ? "is-on" : "is-off");
  els.whitelistCount.textContent = String(self.whitelist_count || 0);
}

async function loadAll() {
  await loadProviders();
  await Promise.all([loadConfig(), loadOverview()]);
}

els.refreshBtn.addEventListener("click", () => loadAll().catch((err) => showToast(err.message || "刷新失败")));
if (els.providerManualBtn) {
  els.providerManualBtn.addEventListener("click", () => {
    if (providerManualMode) {
      const manualValue = els.judgeProviderInput.value.trim();
      syncProviderControl(manualValue);
      if (providerManualMode) showToast("当前 Provider 不在列表中，继续保留手动输入");
      return;
    }
    els.judgeProviderInput.value = els.judgeProviderSelect.value || "";
    setProviderManualMode(true);
  });
}
if (els.judgeProviderSelect) {
  els.judgeProviderSelect.addEventListener("change", () => {
    els.judgeProviderInput.value = "";
  });
}
els.resetPromptBtn.addEventListener("click", () => {
  els.decisionPromptInput.value = els.decisionPromptInput.dataset.defaultPrompt || "";
  renderPromptPreview();
  showToast("已恢复默认提示词，点击保存后生效");
});
els.decisionPromptInput.addEventListener("input", renderPromptPreview);
els.configForm.addEventListener("submit", (event) => saveConfig(event).catch((err) => {
  setSaveState("保存失败", "error");
  showToast(err.message || "保存失败");
}));

// 主题切换：跟随系统 → 浅色 → 深色
if (els.themeToggle) {
  els.themeToggle.addEventListener("click", cycleTheme);
}

// 顶部保存按钮
if (els.saveTopBtn) {
  els.saveTopBtn.addEventListener("click", () => {
    els.configForm.requestSubmit();
  });
}

// 标签页切换
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    const viewId = tab.dataset.view;
    if (!viewId) return;
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    tab.classList.add("active");
    const view = document.getElementById(viewId);
    if (view) view.classList.add("active");
  });
});

loadAll().catch((err) => showToast(err.message || "加载失败"));
