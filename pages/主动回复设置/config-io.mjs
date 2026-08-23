import {
	DEFAULT_CONFIG,
	num,
	parseWhitelist,
	summarizeWhitelist,
} from "./config-form.mjs";
import { isSuccessfulConfigPayload } from "./frontend-core.mjs";
const WHITELIST_ITEM_MAX_LEN = 200;
export const WHITELIST_ILLEGAL_RE = /[\x00-\x1f"'\\]/;
const CONFIG_CONTROL_SELECTOR = "[data-config-key]";

/** POST /config fields are declared by form data-config-key metadata. */
function configControls(form) {
	return [...form.querySelectorAll(CONFIG_CONTROL_SELECTOR)];
}

export function configSaveKeys(form) {
	return configControls(form).map((control) => control.dataset.configKey);
}

function configControlValue(control, providerControls) {
	const { configControl, configKey, configTransform } = control.dataset;
	if (configControl) return providerControls[configControl].value();
	if (configTransform === "whitelist") return parseWhitelist(control.value);
	if (control.type === "checkbox") return control.checked;
	if (control.type === "number")
		return num(control.value, DEFAULT_CONFIG[configKey]);
	return configTransform === "trim" ? control.value.trim() : control.value;
}

export function buildConfigSaveBody(form, providerControls) {
	return Object.fromEntries(
		configControls(form).map((control) => [
			control.dataset.configKey,
			configControlValue(control, providerControls),
		]),
	);
}

function loadConfigControls(form, config, providerControls) {
	for (const control of configControls(form)) {
		const {
			configControl,
			configDefault,
			configFallbackKey,
			configKey,
			configTransform,
		} = control.dataset;
		if (configControl) {
			providerControls[configControl].sync(config[configKey] || "");
		} else if (configTransform === "whitelist") {
			control.value = Array.isArray(config[configKey])
				? config[configKey].join("\n")
				: "";
		} else if (control.type === "checkbox") {
			control.checked =
				configDefault === "true"
					? config[configKey] !== false
					: Boolean(config[configKey]);
		} else if (control.type === "number") {
			control.value = config[configKey] ?? DEFAULT_CONFIG[configKey];
		} else {
			control.value =
				config[configKey] ||
				config[configFallbackKey] ||
				DEFAULT_CONFIG[configKey] ||
				"";
		}
	}
}

export function createConfigIo(deps) {
	const {
		getEls,
		getState,
		setState,
		apiGet,
		apiPost,
		showToast,
		setStatState,
		renderPromptPreview,
		judgeProviderControl,
		visionProviderControl,
		visionJudgeProviderControl,
		fmtBool,
	} = deps;
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
				e.saveTopBtn,
				e.saveMobileBtn,
				e.configForm
					? e.configForm.querySelector('button[type="submit"]')
					: null,
			];
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
				window.setTimeout(
					() => e.navSaveDot.classList.remove("is-pulse"),
					dotMs,
				);
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
		if (e.navSaveState)
			e.navSaveState.textContent = dirty ? "有未保存改动" : "已同步";
		if (dirty && saveStateKind !== "saving")
			setSaveState("有未保存改动", "dirty");
		else if (!dirty && saveStateKind === "dirty") setSaveState("", "");
	}
	function attachDirtyListeners() {
		const e = els();
		if (!e.configForm) return;
		e.configForm.addEventListener("change", () => setDirty(true));
		e.configForm.addEventListener("input", (ev) => {
			if (ev.target.tagName === "INPUT" || ev.target.tagName === "TEXTAREA")
				setDirty(true);
		});
	}
	function setSaving(loading) {
		const e = els();
		const { configLoaded } = getState();
		const buttons = [
			e.saveTopBtn,
			e.saveMobileBtn,
			e.configForm ? e.configForm.querySelector('button[type="submit"]') : null,
		];
		buttons.forEach((btn) => {
			if (!btn) return;
			btn.classList.toggle("is-loading", loading);
			btn.disabled = loading || !configLoaded;
		});
		if (e.refreshBtn) e.refreshBtn.disabled = loading;
	}
	function updateWhitelistFeedback() {
		const e = els();
		if (!e.whitelistInput) return;
		const text = e.whitelistInput.value;
		const count = parseWhitelist(text).length;
		if (e.whitelistCount) e.whitelistCount.textContent = String(count);
		if (e.whitelistSummary)
			e.whitelistSummary.textContent = summarizeWhitelist(text);
	}
	function formatWhitelist() {
		const e = els();
		if (!e.whitelistInput) return;
		const items = parseWhitelist(e.whitelistInput.value);
		const unique = Array.from(new Set(items));
		e.whitelistInput.value = unique.join("\n");
		updateWhitelistFeedback();
		setDirty(true);
		validateWhitelist();
		showToast(
			unique.length ? `已整理并保留 ${unique.length} 个会话` : "白名单已清空",
		);
	}
	function setupValidation() {
		const e = els();
		if (!e.configForm) return;
		numberFields = [];
		e.configForm.querySelectorAll('input[type="number"]').forEach((input) => {
			if (!input.hasAttribute("min") && !input.hasAttribute("max")) return;
			const min = input.hasAttribute("min")
				? Number(input.getAttribute("min"))
				: null;
			const max = input.hasAttribute("max")
				? Number(input.getAttribute("max"))
				: null;
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
		if (e.whitelistInput) {
			e.whitelistInput.addEventListener("input", () => {
				updateWhitelistFeedback();
				validateWhitelist();
			});
			e.whitelistInput.addEventListener("blur", () => validateWhitelist());
		}
		if (e.formatWhitelistBtn) {
			e.formatWhitelistBtn.addEventListener("click", () => formatWhitelist());
		}
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
		else if (field.min != null && value < field.min)
			msg = `不能小于 ${field.min}`;
		else if (field.max != null && value > field.max)
			msg = `不能大于 ${field.max}`;
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
		const bad = lines.find(
			(item) =>
				item.length > WHITELIST_ITEM_MAX_LEN || WHITELIST_ILLEGAL_RE.test(item),
		);
		if (bad) {
			e.whitelistInput.setAttribute("aria-invalid", "true");
			e.whitelistError.textContent =
				bad.length > WHITELIST_ITEM_MAX_LEN
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
		const providerControls = {
			judge: judgeProviderControl,
			vision: visionProviderControl,
			visionJudge: visionJudgeProviderControl,
		};
		loadConfigControls(e.configForm, config, providerControls);
		e.decisionPromptInput.dataset.defaultPrompt =
			config.decision_prompt_default || config.decision_prompt_template || "";
		const whitelist = parseWhitelist(e.whitelistInput.value);
		e.whitelistInput.value = whitelist.join("\n");
		updateWhitelistFeedback();
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
			const body = buildConfigSaveBody(e.configForm, {
				judge: judgeProviderControl,
				vision: visionProviderControl,
				visionJudge: visionJudgeProviderControl,
			});
			const result = await apiPost("config", body);
			if (!result || result.ok !== true) {
				const errorText = result?.error || "保存失败";
				setSaveState("保存失败", "error");
				if (
					String(errorText).includes("非法字符") &&
					e.whitelistInput &&
					e.whitelistError
				) {
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
		if (e.cleanupImageCacheState)
			e.cleanupImageCacheState.textContent = "清理中…";
		try {
			const result = await apiPost("image-cache/cleanup");
			if (!result || result.ok !== true)
				throw new Error(result?.error || "图片缓存清理失败");
			const removed = Number(result.removed || 0);
			if (e.cleanupImageCacheState) {
				e.cleanupImageCacheState.textContent = removed
					? `已清理 ${removed} 个过期图片`
					: "没有需要清理的过期图片";
			}
			showToast(
				removed ? `已清理 ${removed} 个过期图片` : "没有需要清理的过期图片",
			);
		} catch (error) {
			if (e.cleanupImageCacheState)
				e.cleanupImageCacheState.textContent = "清理失败";
			showToast(error.message || "图片缓存清理失败");
		} finally {
			e.cleanupImageCacheBtn.disabled = false;
		}
	}
	return {
		setSaveState,
		setDirty,
		attachDirtyListeners,
		setSaving,
		setupValidation,
		loadConfig,
		saveConfig,
		cleanupImageCache,
	};
}
