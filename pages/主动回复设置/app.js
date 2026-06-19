const PLUGIN_ID = "astrbot_plugin_self_initiated_reply";

const els = {
  refreshBtn: document.getElementById("refreshBtn"),
  saveTopBtn: document.getElementById("saveTopBtn"),
  selfStatus: document.getElementById("selfStatus"),
  whitelistCount: document.getElementById("whitelistCount"),
  configForm: document.getElementById("configForm"),
  enabledInput: document.getElementById("enabledInput"),
  decisionModelInput: document.getElementById("decisionModelInput"),
  providerField: document.querySelector(".provider-field"),
  judgeProviderSelect: document.getElementById("judgeProviderSelect"),
  judgeProviderInput: document.getElementById("judgeProviderInput"),
  providerManualBtn: document.getElementById("providerManualBtn"),
  providerHint: document.getElementById("providerHint"),
  decisionTempInput: document.getElementById("decisionTempInput"),
  decisionTimeoutInput: document.getElementById("decisionTimeoutInput"),
  decisionPromptInput: document.getElementById("decisionPromptInput"),
  promptPreview: document.getElementById("promptPreview"),
  resetPromptBtn: document.getElementById("resetPromptBtn"),
  minContextInput: document.getElementById("minContextInput"),
  idleInput: document.getElementById("idleInput"),
  cooldownInput: document.getElementById("cooldownInput"),
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
  } catch (error) {
    providerOptions = [];
    renderProviderSelect();
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
  els.idleInput.value = config.idle_trigger_seconds ?? 60;
  els.cooldownInput.value = config.cooldown_seconds ?? 180;
  const whitelist = Array.isArray(config.whitelist) ? config.whitelist : [];
  els.whitelistInput.value = whitelist.join("\n");
  els.whitelistCount.textContent = `${whitelist.length} 个白名单`;
  renderPromptPreview();
}

async function saveConfig(event) {
  event.preventDefault();
  els.configSaveState.textContent = "保存中";
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
    idle_trigger_seconds: Number(els.idleInput.value || 60),
    cooldown_seconds: Number(els.cooldownInput.value || 180),
    whitelist,
  });
  if (!result || result.ok !== true) {
    els.configSaveState.textContent = "保存失败";
    showToast(result?.error || "保存失败");
    return;
  }
  els.configSaveState.textContent = "已保存";
  els.whitelistCount.textContent = `${whitelist.length} 个白名单`;
  showToast("配置已保存");
  await loadOverview();
  await loadConfig();
}

async function loadOverview() {
  const overview = await apiGet("unified/overview");
  const self = overview.self_reply || {};
  els.selfStatus.textContent = fmtBool(self.enabled);
  els.whitelistCount.textContent = `${self.whitelist_count || 0} 个白名单`;
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
  els.configSaveState.textContent = "保存失败";
  showToast(err.message || "保存失败");
}));

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
