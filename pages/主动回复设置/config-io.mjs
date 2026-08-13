import { DEFAULT_CONFIG, num, parseWhitelist } from "./config-form.mjs";
import { isSuccessfulConfigPayload } from "./frontend-core.mjs";
const WHITELIST_ITEM_MAX_LEN = 200;
export const WHITELIST_ILLEGAL_RE = /[\x00-\x1f"'\\]/;

/** POST /config 写回字段全集；后端契约测试校验这些键可读写。 */
export const CONFIG_SAVE_KEYS = Object.freeze([
  "enabled",
  "decision_model_enabled",
  "judge_provider_id",
  "decision_temperature",
  "decision_timeout_sec",
  "decision_prompt_template",
  "decision_history_min_messages",
  "message_delay_sec",
  "min_silence_sec",
  "cooldown_sec",
  "vision_judge_enabled",
  "vision_main_enabled",
  "vision_skip_stickers",
  "vision_provider_id",
  "vision_judge_provider_id",
  "vision_max_images",
  "vision_image_age_sec",
  "vision_timeout_sec",
  "proactive_inherit_tools",
  "whitelist_sessions",
]);

export function buildConfigSaveBody(e, controls) {
  const { judgeProviderControl, visionProviderControl, visionJudgeProviderControl } = controls;
  return {
    enabled: e.enabledInput.checked,
    decision_model_enabled: e.decisionModelInput.checked,
    judge_provider_id: judgeProviderControl.value(),
    decision_temperature: num(e.decisionTempInput.value, DEFAULT_CONFIG.decision_temperature),
    decision_timeout_sec: num(e.decisionTimeoutInput.value, DEFAULT_CONFIG.decision_timeout_sec),
    decision_prompt_template: e.decisionPromptInput.value.trim(),
    decision_history_min_messages: num(
      e.minContextInput.value,
      DEFAULT_CONFIG.decision_history_min_messages
    ),
    message_delay_sec: num(e.messageDelayInput.value, DEFAULT_CONFIG.message_delay_sec),
    min_silence_sec: num(e.minSilenceInput.value, DEFAULT_CONFIG.min_silence_sec),
    cooldown_sec: num(e.cooldownInput.value, DEFAULT_CONFIG.cooldown_sec),
    vision_judge_enabled: e.visionJudgeEnabledInput.checked,
    vision_main_enabled: e.visionMainEnabledInput.checked,
    vision_skip_stickers: e.visionSkipStickersInput.checked,
    vision_provider_id: visionProviderControl.value(),
    vision_judge_provider_id: visionJudgeProviderControl.value(),
    vision_max_images: num(e.visionMaxImagesInput.value, DEFAULT_CONFIG.vision_max_images),
    vision_image_age_sec: num(e.visionImageAgeInput.value, DEFAULT_CONFIG.vision_image_age_sec),
    vision_timeout_sec: num(e.visionTimeoutInput.value, DEFAULT_CONFIG.vision_timeout_sec),
    proactive_inherit_tools: e.proactiveInheritToolsInput.checked,
    whitelist_sessions: parseWhitelist(e.whitelistInput.value),
  };
}

export function createConfigIo(deps) {
  const {
    getEls, getState, setState, apiGet, apiPost, showToast, setStatState, renderPromptPreview, judgeProviderControl, visionProviderControl, visionJudgeProviderControl, fmtBool, } = deps;
  let numberFields = [];
  let saveStateKind = "";
  function els() {
    return getEls();
  }
  function setSaveState(message, state) {
    saveStateKind = state;
    const e = els();
    if (!e.configSaveState) return;
    e.configSaveState.textContent = message;
    e.configSaveState.classList.remove("is-pending", "is-ok", "is-error");
    const cssKind = state === "dirty" ? "pending" : state;
    if (cssKind) e.configSaveState.classList.add(`is-${cssKind}`);
    if (e.navSaveState) {
      if (state === "ok") e.navSaveState.textContent = "已保存";
      else if (state === "error") e.navSaveState.textContent = "保存失败";
      else if (state === "saving") e.navSaveState.textContent = "保存中";
    }
    if (e.mobileSaveState) {
      e.mobileSaveState.textContent = message || (state ? "" : "已同步");
      e.mobileSaveState.classList.remove("is-pending", "is-ok", "is-error");
      if (cssKind) e.mobileSaveState.classList.add(`is-${cssKind}`);
    }
    if (state === "ok") {
      const animMs = deps.SAVE_ANIM_MS ?? 1100;
      const dotMs = deps.SAVE_DOT_MS ?? 700;
      const savedButtons = [
        e.saveTopBtn, e.saveMobileBtn, e.configForm ? e.configForm.querySelector('button[type="submit"]') : null, ];
      savedButtons.forEach((btn) => {
        if (!btn) return;
        btn.classList.remove("is-saved");
        void btn.offsetWidth;
        btn.classList.add("is-saved");
        window.setTimeout(() => btn.classList.remove("is-saved"), animMs);
      });
      if (e.navSaveDot) {
        e.navSaveDot.classList.remove("is-pulse");
        void e.navSaveDot.offsetWidth;
        e.navSaveDot.classList.add("is-pulse");
        window.setTimeout(() => e.navSaveDot.classList.remove("is-pulse"), dotMs);
      }
    }
  }
  function setDirty(dirty = true) {
    setState({ isDirty: dirty });
    const e = els();
    if (e.saveTopBtn) e.saveTopBtn.classList.toggle("is-dirty", dirty);
    const bottomSave = e.configForm
      ? e.configForm.querySelector('.form-actions button[type="submit"]')
      : null;
    if (bottomSave) bottomSave.classList.toggle("is-dirty", dirty);
    if (e.navSaveDot) e.navSaveDot.classList.toggle("is-dirty", dirty);
    if (e.mobileSaveBar) e.mobileSaveBar.classList.toggle("is-dirty", dirty);
    if (e.navSaveState) e.navSaveState.textContent = dirty ? "有未保存改动" : "已同步";
    if (dirty && saveStateKind !== "saving") setSaveState("有未保存改动", "dirty");
    else if (!dirty && saveStateKind === "dirty") setSaveState("", "");
  }
  function attachDirtyListeners() {
    const e = els();
    if (!e.configForm) return;
    e.configForm.addEventListener("change", () => setDirty(true));
    e.configForm.addEventListener("input", (ev) => {
      if (ev.target.tagName === "INPUT" || ev.target.tagName === "TEXTAREA") setDirty(true);
    });
  }
  function setSaving(loading) {
    const e = els();
    const { configLoaded } = getState();
    const buttons = [
      e.saveTopBtn, e.saveMobileBtn, e.configForm ? e.configForm.querySelector('button[type="submit"]') : null, ];
    buttons.forEach((btn) => {
      if (!btn) return;
      btn.classList.toggle("is-loading", loading);
      btn.disabled = loading || !configLoaded;
    });
    if (e.refreshBtn) e.refreshBtn.disabled = loading;
  }
  function setupValidation() {
    const e = els();
    if (!e.configForm) return;
    numberFields = [];
    e.configForm.querySelectorAll('input[type="number"]').forEach((input) => {
      if (!input.hasAttribute("min") && !input.hasAttribute("max")) return;
      const min = input.hasAttribute("min") ? Number(input.getAttribute("min")) : null;
      const max = input.hasAttribute("max") ? Number(input.getAttribute("max")) : null;
      const error = document.createElement("span");
      error.className = "field-error";
      error.id = `${input.id}Error`;
      error.setAttribute("role", "alert");
      input.setAttribute("aria-describedby", error.id);
      input.insertAdjacentElement("afterend", error);
      const field = { input, error, min, max };
      numberFields.push(field);
      input.addEventListener("input", () => validateField(field));
      input.addEventListener("blur", () => validateField(field));
    });
  }
  function validateField(field) {
    const raw = field.input.value;
    if (raw === "") {
      field.input.removeAttribute("aria-invalid");
      field.error.classList.remove("show");
      field.error.textContent = "";
      return true;
    }
    const value = Number(raw);
    let msg = "";
    if (!Number.isFinite(value)) msg = "请输入有效数字";
    else if (field.min != null && value < field.min) msg = `不能小于 ${field.min}`;
    else if (field.max != null && value > field.max) msg = `不能大于 ${field.max}`;
    if (msg) {
      field.input.setAttribute("aria-invalid", "true");
      field.error.textContent = msg;
      field.error.classList.add("show");
      return false;
    }
    field.input.removeAttribute("aria-invalid");
    field.error.classList.remove("show");
    field.error.textContent = "";
    return true;
  }
  function validateAll() {
    let firstBad = null;
    for (const field of numberFields) {
      if (!validateField(field) && !firstBad) firstBad = field.input;
    }
    if (firstBad) {
      firstBad.focus();
      return false;
    }
    return true;
  }
  function validateWhitelist() {
    const e = els();
    if (!e.whitelistInput || !e.whitelistError) return true;
    const lines = parseWhitelist(e.whitelistInput.value);
    const bad = lines.find((item) => item.length > WHITELIST_ITEM_MAX_LEN || WHITELIST_ILLEGAL_RE.test(item));
    if (bad) {
      e.whitelistInput.setAttribute("aria-invalid", "true");
      e.whitelistError.textContent = bad.length > WHITELIST_ITEM_MAX_LEN
          ? `条目过长（>${WHITELIST_ITEM_MAX_LEN}）：${bad.slice(0, 24)}…`
          : `条目含非法控制字符：${bad.slice(0, 24)}`;
      e.whitelistError.classList.add("show");
      e.whitelistInput.focus();
      return false;
    }
    e.whitelistInput.removeAttribute("aria-invalid");
    e.whitelistError.classList.remove("show");
    e.whitelistError.textContent = "";
    return true;
  }
  async function loadConfig() {
    const e = els();
    const config = await apiGet("config");
    if (!isSuccessfulConfigPayload(config)) {
      setState({ configLoaded: false });
      setSaving(false);
      throw new Error(config?.error || "配置加载失败");
    }
    e.enabledInput.checked = Boolean(config.enabled);
    e.decisionModelInput.checked = config.decision_model_enabled !== false;
    judgeProviderControl.sync(config.judge_provider_id || "");
    e.decisionTempInput.value = config.decision_temperature ?? DEFAULT_CONFIG.decision_temperature;
    e.decisionTimeoutInput.value = config.decision_timeout_sec ?? DEFAULT_CONFIG.decision_timeout_sec;
    e.decisionPromptInput.value = config.decision_prompt_template || config.decision_prompt_default || "";
    e.decisionPromptInput.dataset.defaultPrompt = config.decision_prompt_default || config.decision_prompt_template || "";
    e.minContextInput.value = config.decision_history_min_messages ?? DEFAULT_CONFIG.decision_history_min_messages;
    e.messageDelayInput.value = config.message_delay_sec ?? DEFAULT_CONFIG.message_delay_sec;
    e.minSilenceInput.value = config.min_silence_sec ?? DEFAULT_CONFIG.min_silence_sec;
    e.cooldownInput.value = config.cooldown_sec ?? DEFAULT_CONFIG.cooldown_sec;
    e.visionJudgeEnabledInput.checked = Boolean(config.vision_judge_enabled);
    e.visionMainEnabledInput.checked = Boolean(config.vision_main_enabled);
    e.visionSkipStickersInput.checked = Boolean(config.vision_skip_stickers);
    visionProviderControl.sync(config.vision_provider_id || "");
    visionJudgeProviderControl.sync(config.vision_judge_provider_id || "");
    e.visionMaxImagesInput.value = config.vision_max_images ?? DEFAULT_CONFIG.vision_max_images;
    e.visionImageAgeInput.value = config.vision_image_age_sec ?? DEFAULT_CONFIG.vision_image_age_sec;
    e.visionTimeoutInput.value = config.vision_timeout_sec ?? DEFAULT_CONFIG.vision_timeout_sec;
    e.proactiveInheritToolsInput.checked = Boolean(config.proactive_inherit_tools);
    const whitelist = Array.isArray(config.whitelist_sessions) ? config.whitelist_sessions : [];
    e.whitelistInput.value = whitelist.join("\n");
    e.whitelistCount.textContent = String(whitelist.length);
    if (e.decisionModelStatus) {
      const decisionOn = config.decision_model_enabled !== false;
      e.decisionModelStatus.textContent = fmtBool(decisionOn);
      setStatState(e.decisionModelStat, decisionOn ? "is-on" : "is-off");
    }
    renderPromptPreview();
    const runtimeOn = config.runtime_enabled !== false;
    e.selfStatus.textContent = config.enabled
      ? runtimeOn
        ? "启用"
        : "已暂停（/off）"
      : "关闭";
    setStatState(e.selfStat, runtimeOn ? "is-on" : "is-off");
    setState({ configLoaded: true });
    setSaving(false);
    setDirty(false);
  }
  async function saveConfig(event) {
    event.preventDefault();
    const state = getState();
    if (state.savingConfig) {
      showToast("正在保存…");
      return;
    }
    if (!state.configLoaded) {
      showToast("配置尚未成功加载，请先刷新页面");
      return;
    }
    if (!validateAll()) {
      showToast("部分数值超出允许范围，请检查标红字段");
      return;
    }
    if (!validateWhitelist()) {
      showToast("白名单有非法条目，请检查标红区域");
      return;
    }
    const e = els();
    setState({ savingConfig: true });
    setSaving(true);
    e.configForm.inert = true;
    e.configForm.classList.add("is-saving");
    setSaveState("保存中", "saving");
    try {
      const body = buildConfigSaveBody(e, {
        judgeProviderControl,
        visionProviderControl,
        visionJudgeProviderControl,
      });
      const result = await apiPost("config", body);
      if (!result || result.ok !== true) {
        const errorText = result?.error || "保存失败";
        setSaveState("保存失败", "error");
        if (String(errorText).includes("非法字符") && e.whitelistInput && e.whitelistError) {
          e.whitelistInput.setAttribute("aria-invalid", "true");
          e.whitelistError.textContent = errorText;
          e.whitelistError.classList.add("show");
          e.whitelistInput.focus();
        }
        showToast(errorText);
        return;
      }
      setSaveState("已保存", "ok");
      setDirty(false);
      e.whitelistCount.textContent = String(body.whitelist_sessions.length);
      showToast("配置已保存");
      try {
        await loadConfig();
      } catch (error) {
        showToast("已保存，但刷新显示失败，请点刷新");
      }
    } finally {
      setState({ savingConfig: false });
      e.configForm.classList.remove("is-saving");
      e.configForm.inert = false;
      setSaving(false);
    }
  }
  async function cleanupImageCache() {
    const e = els();
    if (!e.cleanupImageCacheBtn) return;
    e.cleanupImageCacheBtn.disabled = true;
    if (e.cleanupImageCacheState) e.cleanupImageCacheState.textContent = "清理中…";
    try {
      const result = await apiPost("image-cache/cleanup");
      if (!result || result.ok !== true) throw new Error(result?.error || "图片缓存清理失败");
      const removed = Number(result.removed || 0);
      if (e.cleanupImageCacheState) {
        e.cleanupImageCacheState.textContent = removed
          ? `已清理 ${removed} 个过期图片`
          : "没有需要清理的过期图片";
      }
      showToast(removed ? `已清理 ${removed} 个过期图片` : "没有需要清理的过期图片");
    } catch (error) {
      if (e.cleanupImageCacheState) e.cleanupImageCacheState.textContent = "清理失败";
      showToast(error.message || "图片缓存清理失败");
    } finally {
      e.cleanupImageCacheBtn.disabled = false;
    }
  }
  return {
    setSaveState, setDirty, attachDirtyListeners, setSaving, setupValidation, validateAll, validateWhitelist, loadConfig, saveConfig, cleanupImageCache, };
}
