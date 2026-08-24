const PLUGIN_ID = "astrbot_plugin_self_initiated_reply";
window.__selfreplyAppStarted = true;
if (window.__selfreplyBootFailTimer)
	window.clearTimeout(window.__selfreplyBootFailTimer);
import {
	FETCH_TIMEOUT_MS,
	createConfigRequestCoordinator,
	requestPluginApi,
} from "./frontend-core.mjs";
import { renderPromptTemplateHtml } from "./config-form.mjs";
import { createProviderControl } from "./providers.mjs";
import {
	THEME_KEY,
	applyTheme,
	currentTheme,
	nextTheme,
	persistTheme,
	restoreTheme,
} from "./theme.mjs";
import {
	bindDimBoldButtons,
	createScrollHandler,
	hideBoot,
	restoreDimBold,
	setupMobileTabs,
	setupMoreActionsMenu,
	setupNav,
	updateNavFades,
	updateTopbarStuck,
} from "./chrome.mjs";
import { createConfigIo } from "./config-io.mjs";

let els = null;
const $ = (id) => document.getElementById(id);

function getEls() {
	if (els) return els;
	els = {
		topbar: document.querySelector(".topbar"),
		sidenavList: document.querySelector(".sidenav-list"),
		navSaveDot: $("navSaveDot"),
		navSaveState: $("navSaveState"),
		refreshBtn: $("refreshBtn"),
		saveTopBtn: $("saveTopBtn"),
		themeToggle: $("themeToggle"),
		selfStat: $("selfStat"),
		selfStatus: $("selfStatus"),
		decisionModelStat: $("decisionModelStat"),
		decisionModelStatus: $("decisionModelStatus"),
		whitelistCount: $("whitelistCount"),
		configForm: $("configForm"),
		enabledInput: $("enabledInput"),
		decisionModelInput: $("decisionModelInput"),
		providerField: $("judgeProviderField"),
		judgeProviderSelect: $("judgeProviderSelect"),
		judgeProviderInput: $("judgeProviderInput"),
		providerManualBtn: $("providerManualBtn"),
		visionProviderSelect: $("visionProviderSelect"),
		visionProviderInput: $("visionProviderInput"),
		visionProviderManualBtn: $("visionProviderManualBtn"),
		visionJudgeProviderSelect: $("visionJudgeProviderSelect"),
		visionJudgeProviderInput: $("visionJudgeProviderInput"),
		visionJudgeProviderManualBtn: $("visionJudgeProviderManualBtn"),
		providerHint: $("providerHint"),
		providerListState: $("providerListState"),
		decisionPromptInput: $("decisionPromptInput"),
		promptPreview: $("promptPreview"),
		resetPromptBtn: $("resetPromptBtn"),
		cleanupImageCacheBtn: $("cleanupImageCacheBtn"),
		cleanupImageCacheState: $("cleanupImageCacheState"),
		whitelistInput: $("whitelistInput"),
		whitelistError: $("whitelistError"),
		whitelistSummary: $("whitelistSummary"),
		formatWhitelistBtn: $("formatWhitelistBtn"),
		configSaveState: $("configSaveState"),
		toast: $("toast"),
		boot: $("boot"),
		mobileSaveBar: $("mobileSaveBar"),
		mobileSaveState: $("mobileSaveState"),
		saveMobileBtn: $("saveMobileBtn"),
		mobileTabbar: $("mobileTabbar"),
		moreActions: $("moreActions"),
		moreActionsBtn: $("moreActionsBtn"),
		moreActionsMenu: $("moreActionsMenu"),
	};
	return els;
}

getEls();

let bridgeReady = null;
let providerOptions = [];
let providerListAvailable = false;
const state = {
	savingConfig: false,
	configLoaded: false,
	configRevision: "",
	runtimeEnabled: false,
	requiresConfigRefresh: false,
	isDirty: false,
};
const configRequestCoordinator = createConfigRequestCoordinator();
const REFRESH_ARM_MS = 3000;
const TOAST_MS = 2200;
const SAVE_ANIM_MS = 1100;
const SAVE_DOT_MS = 700;
const PREVIEW_DEBOUNCE_MS = 80;
const BOOT_TIMEOUT_MS = 12000;

function setStatState(element, stateName) {
	if (!element) return;
	element.classList.remove("is-on", "is-off", "is-info");
	element.classList.add(stateName);
}

function showToast(message) {
	if (!els.toast) return;
	els.toast.textContent = message;
	els.toast.classList.add("show");
	window.clearTimeout(showToast.timer);
	showToast.timer = window.setTimeout(
		() => els.toast.classList.remove("show"),
		TOAST_MS,
	);
}

function debounce(fn, delay) {
	let timer = null;
	return (...args) => {
		window.clearTimeout(timer);
		timer = window.setTimeout(() => fn(...args), delay);
	};
}

async function getBridge() {
	if (!window.AstrBotPluginPage) return null;
	if (!bridgeReady)
		bridgeReady = window.AstrBotPluginPage.ready().catch(() => null);
	await bridgeReady;
	return window.AstrBotPluginPage;
}

function apiGet(endpoint, params = {}) {
	return requestPluginApi({
		getBridge,
		pluginId: PLUGIN_ID,
		endpoint,
		method: "GET",
		params,
		fetchImpl: window.fetch.bind(window),
		pageUrl: window.location.href,
		timeoutMs: FETCH_TIMEOUT_MS,
	});
}

function apiPost(endpoint, body = {}) {
	return requestPluginApi({
		getBridge,
		pluginId: PLUGIN_ID,
		endpoint,
		method: "POST",
		body,
		fetchImpl: window.fetch.bind(window),
		pageUrl: window.location.href,
		timeoutMs: FETCH_TIMEOUT_MS,
	});
}

function fmtBool(value) {
	return value ? "启用" : "关闭";
}

function renderPromptPreview() {
	if (!els.promptPreview) return;
	const template =
		els.decisionPromptInput.value ||
		els.decisionPromptInput.dataset.defaultPrompt ||
		"";
	els.promptPreview.innerHTML = renderPromptTemplateHtml(template);
}

const providerDeps = {
	getOptions: () => providerOptions,
	isListAvailable: () => providerListAvailable,
	showToast: (msg) => showToast(msg),
};

const visionProviderControl = createProviderControl(
	{
		select: els.visionProviderSelect,
		input: els.visionProviderInput,
		button: els.visionProviderManualBtn,
		placeholder: "使用当前会话模型",
	},
	providerDeps,
);

const visionJudgeProviderControl = createProviderControl(
	{
		select: els.visionJudgeProviderSelect,
		input: els.visionJudgeProviderInput,
		button: els.visionJudgeProviderManualBtn,
		placeholder: "与识图模型一致",
	},
	providerDeps,
);

const judgeProviderControl = createProviderControl(
	{
		select: els.judgeProviderSelect,
		input: els.judgeProviderInput,
		button: els.providerManualBtn,
		placeholder: "使用当前会话默认模型",
	},
	{
		...providerDeps,
		onModeChange: (manual) => {
			if (els.providerField)
				els.providerField.classList.toggle("manual", manual);
			if (els.providerHint) {
				els.providerHint.textContent = manual
					? "手动输入为空时使用当前会话默认模型"
					: "留空表示使用当前会话默认模型";
			}
		},
	},
);

const configIo = createConfigIo({
	getEls: () => els,
	getState: () => state,
	setState: (patch) => Object.assign(state, patch),
	apiGet,
	apiPost,
	showToast,
	setStatState,
	renderPromptPreview,
	judgeProviderControl,
	visionProviderControl,
	visionJudgeProviderControl,
	fmtBool,
	requestCoordinator: configRequestCoordinator,
	SAVE_ANIM_MS,
	SAVE_DOT_MS,
});

async function loadProviders() {
	try {
		const result = await apiGet("providers");
		if (!result || result.ok === false)
			throw new Error(result?.error || "无法加载 Provider 列表");
		providerOptions = Array.isArray(result.providers)
			? result.providers.filter((item) => item && item.id)
			: [];
		providerListAvailable = true;
		judgeProviderControl.render();
		visionProviderControl.render();
		visionJudgeProviderControl.render();
		if (els.providerListState) els.providerListState.textContent = "";
		return { listAvailable: true };
	} catch (error) {
		providerOptions = [];
		providerListAvailable = false;
		judgeProviderControl.render();
		visionProviderControl.render();
		visionJudgeProviderControl.render();
		judgeProviderControl.setManual(true);
		visionProviderControl.setManual(true);
		visionJudgeProviderControl.setManual(true);
		if (els.providerListState) {
			els.providerListState.textContent =
				"Provider 列表不可用，三个 Provider 均可手动填写";
		}
		showToast("无法加载 Provider 列表，可手动填写");
		return { listAvailable: false };
	}
}

async function loadAll({ force = false } = {}) {
	await loadProviders();
	await configIo.loadConfig({ force });
}

let refreshing = false;
let refreshArmed = false;
let refreshArmTimer = null;

async function doRefresh() {
	if (state.savingConfig || refreshing) return;
	refreshing = true;
	els.refreshBtn.disabled = true;
	els.refreshBtn.classList.add("is-loading");
	try {
		await loadAll({ force: true });
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
		if (state.isDirty && !refreshArmed) {
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
	els.cleanupImageCacheBtn.addEventListener("click", () =>
		configIo.cleanupImageCache(),
	);
}

if (els.resetPromptBtn) {
	els.resetPromptBtn.addEventListener("click", () => {
		if (!state.configLoaded) {
			showToast("配置尚未成功加载，请先刷新页面");
			return;
		}
		els.decisionPromptInput.value =
			els.decisionPromptInput.dataset.defaultPrompt || "";
		renderPromptPreview();
		configIo.setDirty(true);
		showToast("已恢复默认提示词，点击保存后生效");
	});
}

if (els.decisionPromptInput) {
	els.decisionPromptInput.addEventListener(
		"input",
		debounce(renderPromptPreview, PREVIEW_DEBOUNCE_MS),
	);
}

if (els.enabledInput) {
	els.enabledInput.addEventListener("change", () => {
		els.selfStatus.textContent = els.enabledInput.checked
			? "启用（未保存）"
			: "关闭（未保存）";
		configIo.setDirty(true);
	});
}

if (els.decisionModelInput) {
	els.decisionModelInput.addEventListener("change", () => {
		const on = els.decisionModelInput.checked;
		els.decisionModelStatus.textContent = fmtBool(on);
		setStatState(els.decisionModelStat, on ? "is-on" : "is-off");
		configIo.setDirty(true);
	});
}

if (els.configForm) {
	els.configForm.addEventListener("submit", (event) =>
		configIo.saveConfig(event).catch((err) => {
			configIo.setSaveState("保存失败", "error");
			showToast(err.message || "保存失败");
		}),
	);
}

if (els.themeToggle) {
	els.themeToggle.addEventListener("click", () => {
		const next = nextTheme();
		applyTheme(next, els.themeToggle);
		persistTheme(next, apiPost);
	});
}

bindDimBoldButtons();
restoreDimBold();
try {
	const saved = localStorage.getItem(THEME_KEY);
	if (saved === "light" || saved === "dark") applyTheme(saved, els.themeToggle);
} catch (error) {
	/* localStorage 不可用 */
}

if (els.saveTopBtn) {
	els.saveTopBtn.addEventListener("click", () =>
		els.configForm.requestSubmit(),
	);
}
if (els.saveMobileBtn) {
	els.saveMobileBtn.addEventListener("click", () =>
		els.configForm.requestSubmit(),
	);
}

setupNav(els);
configIo.setupValidation();
setupMoreActionsMenu(els);
setupMobileTabs(els);
window.addEventListener("scroll", createScrollHandler(els), { passive: true });
updateTopbarStuck(els);
configIo.attachDirtyListeners();

if (els.sidenavList) {
	els.sidenavList.addEventListener("scroll", () => updateNavFades(els), {
		passive: true,
	});
	window.addEventListener("resize", () => updateNavFades(els), {
		passive: true,
	});
}

window.addEventListener("beforeunload", (e) => {
	if (state.isDirty) {
		e.preventDefault();
		e.returnValue = "";
	}
});

getEls();
configIo.setSaving(false);

const bootTimeout = window.setTimeout(() => {
	hideBoot(els);
	showToast("加载超时，请刷新页面或检查后端状态");
}, BOOT_TIMEOUT_MS);

loadAll()
	.then(() => {
		window.clearTimeout(bootTimeout);
		hideBoot(els);
	})
	.catch((err) => {
		window.clearTimeout(bootTimeout);
		state.configLoaded = false;
		configIo.setSaving(false);
		hideBoot(els);
		showToast(err.message || "加载失败");
	});

restoreTheme(apiGet).then((theme) => {
	if (theme !== currentTheme()) applyTheme(theme, els.themeToggle);
});
