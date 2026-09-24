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
	applyBold,
	applyDim,
	bindDimBoldButtons,
	createScrollHandler,
	dimBoldWasTouched,
	hideBoot,
	markThemeTouched,
	restoreDimBold,
	setupMobileTabs,
	setupMoreActionsMenu,
	setupNav,
	syncTopbarHeight,
	themeWasTouched,
	updateNavFades,
	updateTopbarStuck,
} from "./chrome.mjs";
import { createConfigIo } from "./config-io.mjs";

const PLUGIN_ID = "astrbot_plugin_self_initiated_reply";
window.__selfreplyAppStarted = true;
if (window.__selfreplyBootFailTimer)
	window.clearTimeout(window.__selfreplyBootFailTimer);

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
		saveBottomBtn: $("saveBottomBtn"),
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
const PREVIEW_DEBOUNCE_MS = 80;
const BOOT_TIMEOUT_MS = 12000;
let toastTimer = null;

function setStatState(element, stateName) {
	if (!element) return;
	element.classList.remove("is-on", "is-off", "is-info");
	element.classList.add(stateName);
}

function showToast(message, isError = false) {
	const toast = els.toast;
	if (!toast) return;
	toast.textContent = message;
	// role 恒为 status + aria-live="polite"（静态声明在 index.html）：紧急通道由
	// 带 role="alert" 的字段级错误承担，toast 不切 role——role="alert" 与显式
	// aria-live="polite" 是互相矛盾的组合（显式值优先，alert 的紧急语义被中和）。
	// 错误态只用 is-error 表达视觉差异，不改变朗读优先级。
	toast.classList.toggle("is-error", Boolean(isError));
	toast.classList.add("show");
	window.clearTimeout(toastTimer);
	toastTimer = window.setTimeout(() => toast.classList.remove("show"), TOAST_MS);
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

function request(method, endpoint, payload = {}) {
	return requestPluginApi({
		getBridge,
		pluginId: PLUGIN_ID,
		endpoint,
		method,
		params: method === "GET" ? payload : {},
		body: method === "POST" ? payload : {},
		fetchImpl: window.fetch.bind(window),
		pageUrl: window.location.href,
	});
}

function apiGet(endpoint, params = {}) {
	return request("GET", endpoint, params);
}

function apiPost(endpoint, body = {}) {
	return request("POST", endpoint, body);
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
	onDirty: () => configIo.setDirty(true),
};

/* 三个 Provider 控件同构。差异只在占位文案，以及 judge 要切换容器 class 与 hint。
   元素在此直接取：它们不参与其余逻辑，不必再进 getEls。 */
const PROVIDER_CONTROLS = [
	{
		name: "vision",
		select: $("visionProviderSelect"),
		input: $("visionProviderInput"),
		button: $("visionProviderManualBtn"),
		placeholder: "使用当前会话模型",
	},
	{
		name: "visionJudge",
		select: $("visionJudgeProviderSelect"),
		input: $("visionJudgeProviderInput"),
		button: $("visionJudgeProviderManualBtn"),
		placeholder: "与识图模型一致",
	},
	{
		name: "judge",
		select: $("judgeProviderSelect"),
		input: $("judgeProviderInput"),
		button: $("providerManualBtn"),
		placeholder: "使用当前会话默认模型",
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
];

const providerControlsByName = {};
const providerControlList = PROVIDER_CONTROLS.map((spec) => {
	const control = createProviderControl(
		spec,
		spec.onModeChange ? { ...providerDeps, onModeChange: spec.onModeChange } : providerDeps,
	);
	providerControlsByName[spec.name] = control;
	return control;
});
const visionProviderControl = providerControlsByName.vision;
const visionJudgeProviderControl = providerControlsByName.visionJudge;
const judgeProviderControl = providerControlsByName.judge;

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
	getProviderOptions: () => providerOptions,
	isProviderListAvailable: () => providerListAvailable,
});

async function loadProviders() {
	const renderAll = () => providerControlList.forEach((control) => control.render());
	try {
		const result = await apiGet("providers");
		if (!result || result.ok === false)
			throw new Error(result?.error || "无法加载 Provider 列表");
		providerOptions = Array.isArray(result.providers)
			? result.providers.filter((item) => item && item.id)
			: [];
		providerListAvailable = true;
		renderAll();
		if (els.providerListState) els.providerListState.textContent = "";
		return { listAvailable: true };
	} catch (error) {
		providerOptions = [];
		providerListAvailable = false;
		// 顺序是硬约束：control.render() 会先 innerHTML="" 再写回旧值，列表为空时
		// 写回即归零（实测 beforeRender="provider-a" → afterRender=""）。故当前值
		// 必须在 renderAll() **之前**取；render 之后同步交给 sync()——此时
		// isListAvailable() 为 false，它必然落到手动分支，把值写进输入框（该输入框
		// 此后既是显示来源也是 buildConfigSaveBody 的取值来源）。反过来写等于没改。
		const preserved = providerControlList.map((control) => control.value());
		renderAll();
		// 列表不可用时全部切手动输入：循环保证不会漏掉后加的控件。
		providerControlList.forEach((control, index) => control.sync(preserved[index]));
		if (els.providerListState) {
			els.providerListState.textContent =
				"Provider 列表不可用，三个 Provider 均可手动填写";
		}
		showToast("无法加载 Provider 列表，可手动填写");
		return { listAvailable: false };
	}
}

// 首屏加载是否仍在途：boot 看门狗据此让位，避免串行两个请求（各 15s 上限，
// 最坏 30s）超过 12s 定时器时误报「加载超时」。doRefresh 不参与此标志。
let loadInFlight = false;

async function loadAll({ force = false } = {}) {
	loadInFlight = true;
	try {
		await loadProviders();
		// 透传 loadConfig 的结果：false 表示这次响应被协调器拦下（表单已脏或
		// 有编辑在此请求期间发生），配置并没有被替换。
		return await configIo.loadConfig({ force });
	} finally {
		loadInFlight = false;
	}
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
		const applied = await loadAll({ force: true });
		// 只说真话：表单脏时响应被拦下，内容仍是用户编辑的那份，谎报「已刷新为
		// 最新配置」会让用户以为磁盘内容已生效（随后保存会把他的编辑覆盖上去）。
		showToast(
			applied === false
				? "检测到未保存改动，已保留当前内容，请保存后再刷新"
				: "已刷新为最新配置",
		);
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
			showToast(configIo.configNotLoadedMessage());
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
		markThemeTouched();
		applyTheme(next, els.themeToggle);
		persistTheme(next, apiPost);
	});
}

bindDimBoldButtons(() => persistTheme(null, apiPost));
restoreDimBold();
try {
	const saved = localStorage.getItem(THEME_KEY);
	if (saved === "light" || saved === "dark") applyTheme(saved, els.themeToggle);
} catch (error) {
	/* localStorage 不可用 */
}

// 两个显式保存按钮都走原生提交（底部按钮 type="submit" 同理）：同一语义一种实现。
[els.saveTopBtn, els.saveMobileBtn].forEach((btn) => {
	if (btn) btn.addEventListener("click", () => els.configForm.requestSubmit());
});

setupNav(els);
configIo.setupValidation();
setupMoreActionsMenu(els);
setupMobileTabs(els);
window.addEventListener("scroll", createScrollHandler(els), { passive: true });
syncTopbarHeight(els);
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

configIo.setSaving(false);

// 看门狗只兜底：加载成功/失败均由 loadAll 收尾反馈；仅当加载已收尾且未成功、
// 或首屏串行请求总预算（2 × FETCH_TIMEOUT_MS）已被 12s 定时器追平时才报失败。
// 定时器句柄存在可变量里：再查分支会重写句柄，收尾路径才能取消最新的那个。
let bootTimeout = window.setTimeout(function checkBoot() {
	if (state.configLoaded) return;
	if (loadInFlight) {
		// 仍在途：每个请求自身有 15s 硬上限、首屏至多两个串行请求，必然收敛；
		// 再查一次即可覆盖第二个请求的窗口，不会无限顺延。
		bootTimeout = window.setTimeout(checkBoot, FETCH_TIMEOUT_MS);
		return;
	}
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

restoreTheme(apiGet).then((prefs) => {
	if (!themeWasTouched() && prefs.theme !== currentTheme())
		applyTheme(prefs.theme, els.themeToggle);
	if (!dimBoldWasTouched()) {
		applyDim(prefs.dim);
		applyBold(prefs.bold);
	}
});
